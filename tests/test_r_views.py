"""R4: CloudState (warm pool, t_cold_p90, blocks, breaker, HALTED/KILL, budget stage, weights hold,
5 s cache) and retry_after_s - real DB, scenarios inserted as rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llmhub.clock import FakeClock
from llmhub.cloud_manager.provisioner import set_weights_hold
from llmhub.configuration.api_models import LocalBackendConfig, ModelAliasConfig
from llmhub.configuration.models import Config, PoolConfig
from llmhub.lifecycle_manager.safety import halt
from llmhub.model_router.rules import LocalView, RouteRequest, decide
from llmhub.model_router.views import CloudState, retry_after_s
from llmhub.persistence.db import connect, migrate
from llmhub.task_queue.models import Priority

CLOCK0 = 1_793_750_400.0


def make_cfg(tmp_path: Path) -> Config:
    pool = PoolConfig(
        name="p1", provider="mock", gpu="g", vram_gb=24, pp_tps=1000, tg_tps=50, image="i",
        disk_gb=50, models=["m1"], cold_start_s_default=540, max_price_usd_h=0.36,
    )  # fmt: skip
    return Config(
        data_dir=tmp_path,
        pools={"p1": pool},
        local_backends={"b": LocalBackendConfig(url="http://x/v1", model="m", ctx_tokens=4096, pp_tps=10, tg_tps=5)},
        models={"m1": ModelAliasConfig(local="b", cloud_ctx_tokens=65536), "solo": ModelAliasConfig(local="b")},
    )  # fmt: skip


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.cfg = make_cfg(tmp_path)
        self.clock = FakeClock(CLOCK0)
        self.conn: sqlite3.Connection = connect(":memory:")
        migrate(self.conn)
        self.state = CloudState(self.conn, self.cfg, self.clock)
        self.n = 0

    def instance(
        self, st: str, *, created: float | None = None, ready: float | None = None
    ) -> None:
        self.n += 1
        t = CLOCK0 if created is None else created
        self.conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, created_at,"
            " ready_at, max_lifetime_at, price_usd_h) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"i{self.n}",
                f"op{self.n}",
                "p1",
                "mock",
                f"l{self.n}",
                st,
                t,
                t,
                ready,
                t + 9999,
                0.72,
            ),
        )

    def account(self, **cols: object) -> None:
        names = ["provider", *cols]
        self.conn.execute(
            f"INSERT OR REPLACE INTO provider_accounts({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
            ["mock", *cols.values()],
        )

    def spend(self, usd: float) -> None:
        self.conn.execute(
            "INSERT INTO ledger(provider, pool, kind, from_ts, to_ts, amount_usd, source, created_at)"
            " VALUES ('mock','p1','gpu',?,?,?, 'estimate', 0)",
            (CLOCK0 + 10, CLOCK0 + 70, usd),
        )

    def view(self, prio: Priority = Priority.NORMAL, alias: str = "m1"):
        self.clock._now += 6  # past the 5 s cache
        return self.state.view(alias, 1000, 500, prio)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.mark.parametrize("st", ["READY", "IDLE", "BUSY"])
def test_warm_states(env: Env, st: str) -> None:
    env.instance(st)
    v = env.view()
    assert v.warm and v.unavailable is None and v.pool == "p1" and v.ctx_tokens == 65536
    assert v.eta_s == pytest.approx(1000 / 1000 + 500 / 50)


@pytest.mark.parametrize("st", ["STOPPED", "PROVISIONING", "BOOTING", "LOADING_MODEL", "STOPPING"])
def test_not_warm_states(env: Env, st: str) -> None:
    env.instance(st)
    assert not env.view().warm


def test_cold_eta_uses_default_then_p90(env: Env) -> None:
    v = env.view()
    assert not v.warm and v.eta_s == pytest.approx(540 + 11)
    for d in (100, 120, 140, 160, 180, 200, 220, 240, 260, 300):
        env.instance("STOPPED", created=CLOCK0 - 5000 + d, ready=CLOCK0 - 5000 + 2 * d)
    assert env.view().eta_s == pytest.approx(260 + 11)  # p90 of the durations 100..300


def test_cost_cold_includes_boot_warm_does_not(env: Env) -> None:
    cold = env.view()
    assert cold.est_cost_usd == pytest.approx(0.36 * (540 + 11) / 3600)
    env.instance("IDLE")
    assert env.view().est_cost_usd == pytest.approx(0.72 * 11 / 3600)


def test_alias_without_pool(env: Env) -> None:
    v = env.view(alias="solo")
    assert v.pool is None and v.unavailable is None


@pytest.mark.parametrize("reason", ["insufficient_balance", "quota_exceeded", "provider_api_error"])
def test_blocked_reason(env: Env, reason: str) -> None:
    env.account(blocked_reason=reason)
    assert env.view().unavailable == reason


def test_unknown_block_reason_maps_to_provider_api_error(env: Env) -> None:
    env.account(blocked_reason="weird")
    assert env.view().unavailable == "provider_api_error"


def test_circuit_open_gives_retry_after(env: Env) -> None:
    env.account(circuit_state="open", circuit_opened_at=CLOCK0 - 100)
    v = env.view()
    assert v.unavailable == "provider_api_error"
    assert v.retry_after_s == pytest.approx(900 - 106)


def test_circuit_open_expired_is_available(env: Env) -> None:
    env.account(circuit_state="open", circuit_opened_at=CLOCK0 - 1000)
    assert env.view().unavailable is None
    env.account(circuit_state="half_open", circuit_opened_at=CLOCK0 - 10)
    assert env.view().unavailable is None


def test_halted_and_kill_file(env: Env, tmp_path: Path) -> None:
    halt(env.conn, "test", CLOCK0)
    assert env.view().unavailable == "controller_halted"
    env.conn.execute("UPDATE controller_state SET value='0' WHERE key='halted'")
    assert env.view().unavailable is None
    (tmp_path / "KILL").write_text("")
    assert env.view().unavailable == "controller_halted"


def test_no_adapter_for_provider(env: Env) -> None:
    env.state = CloudState(env.conn, env.cfg, env.clock, has_adapter=lambda _p: False)
    assert env.view().unavailable == "provider_api_error"


@pytest.mark.parametrize(
    ("spent", "stage_name"),
    [(0.5, "NORMAL"), (2.5, "ECONOMY"), (2.9, "URGENT_ONLY"), (3.0, "NO_LAUNCH")],
)  # budget_day = 3
def test_budget_stage_reported(env: Env, spent: float, stage_name: str) -> None:
    env.spend(spent)
    env.clock._now += 6
    assert env.state.budget_stage().name == stage_name


def test_economy_stage_keeps_cloud_available(env: Env) -> None:
    env.spend(2.5)
    assert env.view().unavailable is None


def test_p5_urgent_only_cloud_for_urgent_only_warm_or_cold(env: Env) -> None:
    env.spend(2.9)
    env.instance("READY")  # warm pool too: ADR-0138 95 % -> cloud only for urgent
    assert env.view(Priority.NORMAL).unavailable == "budget_limit_reached"
    assert env.view(Priority.LOW).unavailable == "budget_limit_reached"
    assert env.view(Priority.URGENT).unavailable is None
    env.conn.execute("UPDATE instances SET state='STOPPED'")
    assert env.view(Priority.NORMAL).unavailable == "budget_limit_reached"
    assert env.view(Priority.URGENT).unavailable is None


def test_f3_no_launch_closes_cloud_for_all_priorities(env: Env) -> None:
    env.spend(3.0)
    for warm in (False, True):
        if warm:
            env.instance("READY")
        for p in Priority:
            assert env.view(p).unavailable == "budget_limit_reached"


def test_weights_hold_blocks_cold_only(env: Env) -> None:
    set_weights_hold(env.conn, "p1", CLOCK0, env.cfg, "weights_failed:sha_mismatch")
    v = env.view()
    assert (
        v.unavailable == "deployment_error" and v.retry_after_s is not None and v.retry_after_s > 0
    )
    env.instance("IDLE")
    assert env.view().unavailable is None


def test_global_part_cached_for_5s(env: Env) -> None:
    assert env.state.view("m1", 1, 1, Priority.NORMAL).unavailable is None
    env.account(blocked_reason="quota_exceeded")
    assert env.state.view("m1", 1, 1, Priority.NORMAL).unavailable is None  # still cached
    env.clock._now += 4.9
    assert env.state.view("m1", 1, 1, Priority.NORMAL).unavailable is None
    env.clock._now += 0.2
    assert env.state.view("m1", 1, 1, Priority.NORMAL).unavailable == "quota_exceeded"
    env.account(blocked_reason=None)
    env.state.invalidate()
    assert env.state.view("m1", 1, 1, Priority.NORMAL).unavailable is None


def test_view_feeds_decide_end_to_end(env: Env) -> None:
    env.account(blocked_reason="insufficient_balance")
    cloud = env.view()
    local = LocalView("b", 4096, 30.0, True)
    req = RouteRequest("m1", None, Priority.NORMAL, "QUALITY", None, "cloud_allowed", (), 1000, 500)
    d = decide(req, local, cloud, env.cfg.router)
    assert (d.route, d.fallback) == ("local", "insufficient_balance")


# ---- retry_after_s -----------------------------------------------------------------------
class FakeEta:
    def __init__(self, eta: float | None) -> None:
        self.eta = eta
        self.calls: list[tuple[object, ...]] = []

    def eta_s(
        self, alias: str, tokens_in: int, max_tokens: int, prio_rank: int, now: float
    ) -> float | None:
        self.calls.append((alias, tokens_in, max_tokens, prio_rank))
        return self.eta


def job(pool: str | None, tin: int = 1000, tout: int = 500) -> dict[str, object]:
    return {
        "pool": pool,
        "model_alias": "m1",
        "est_tokens_in": tin,
        "est_tokens_out": tout,
        "prio_rank": 1,
    }


def ra(env: Env, j: dict[str, object], worker: FakeEta) -> int:
    class Row(dict):  # sqlite3.Row stand-in
        pass

    return retry_after_s(env.conn, env.cfg, Row(j), worker, CLOCK0)  # type: ignore[arg-type]


def test_retry_after_local_uses_worker_eta_and_clamps(env: Env) -> None:
    w = FakeEta(123.2)
    assert ra(env, job(None), w) == 124
    assert w.calls == [("m1", 1000, 500, 1)]
    assert ra(env, job(None), FakeEta(1.0)) == 5
    assert ra(env, job(None), FakeEta(99999.0)) == 900
    assert ra(env, job(None), FakeEta(None)) == 900


def test_retry_after_cloud_cold_and_warm(env: Env) -> None:
    assert ra(env, job("p1"), FakeEta(None)) == 551  # 540 + 1000/1000 + 500/50
    env.instance("IDLE")
    assert ra(env, job("p1"), FakeEta(None)) == 11
    assert ra(env, job("p1", 0, 0), FakeEta(None)) == 5
