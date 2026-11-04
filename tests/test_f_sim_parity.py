"""F9: simulator parity (marker slow). Traces S1-S6 and RF2/RF4/RF6 (3 seeds, strategies
`idle 5m` and `batch 5/30m drain`) from tests/fixtures/sim/*.json run through the real
Controller on Mock with a FakeClock: the number of launches must match EXACTLY and the cost
within +-5 %. Settings (plan section 9, F9): boot 0 + load = startup, instant destroy, no lag,
pp/tg from the simulator Params, deadline_margin 0, deadline_includes_work=False,
max_concurrency 1, budget limits lifted, max_lifetime beyond the horizon (validator bypassed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness import Harness
from llmhub.configuration.models import (
    BudgetConfig,
    Config,
    PoolConfig,
    QueueClassConfig,
    QueueConfig,
    TimeoutsConfig,
)
from llmhub.provider_adapters.mock import MockKnobs
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
from llmhub.task_queue.models import JobSpec, Priority

pytestmark = pytest.mark.slow

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "sim").glob("*.json"))
UNLIMITED = 1e6
INSTANT = QueueClassConfig(minimum_batch_size=1, maximum_batch_wait_s=0, maximum_queue_wait_s=0)


def parity_config(data_dir: Path, fx: dict[str, Any]) -> Config:
    p, st = fx["params"], fx["strategy"]
    pool = PoolConfig(
        name="p1", provider="mock", gpu="SIM", vram_gb=24, max_price_usd_h=p["price_per_h"],
        idle_timeout_s=st["idle_s"], max_concurrency=1, pp_tps=p["prefill_tps"] or 1e12,
        tg_tps=p["gen_tps"], cold_start_s_default=p["startup_s"], image="llmhub-infer:sim",
        disk_gb=10, models=["m1"],
    )  # fmt: skip
    if st["min_batch"] > 1:
        batch = QueueClassConfig(
            minimum_batch_size=st["min_batch"], maximum_batch_wait_s=st["max_batch_wait_s"],
            maximum_queue_wait_s=st["max_queue_wait_s"] or 0,
        )  # fmt: skip
        queue = QueueConfig(urgent=INSTANT, normal=batch, low=batch, deadline_margin_s=0,
                            deadline_includes_work=False)  # fmt: skip
    else:
        queue = QueueConfig(urgent=INSTANT, normal=INSTANT, low=INSTANT, deadline_margin_s=0,
                            deadline_includes_work=False)  # fmt: skip
    # max_lifetime beyond the horizon: the production validator (<= 3 h) is bypassed here
    timeouts = TimeoutsConfig().model_copy(
        update={"max_lifetime_s": 10 * 86400, "worker_heartbeat_s": 3600, "worker_stale_s": 7200}
    )
    budget = BudgetConfig(
        hard_cap_month=UNLIMITED, budget_month=UNLIMITED, budget_day=UNLIMITED,
        budget_session=UNLIMITED, budget_job=UNLIMITED, reserve_usd=0,
    )  # fmt: skip
    return Config(data_dir=data_dir, pools={"p1": pool}, queue=queue, timeouts=timeouts,
                  budget=budget)  # fmt: skip


def _priority(prio: int, batching: bool) -> Priority:
    if prio == 0:
        return Priority.URGENT
    if batching:  # the simulator counts one batch over all non-urgent requests
        return Priority.LOW
    return Priority.NORMAL if prio == 1 else Priority.LOW


async def run_fixture(tmp_path: Path, fx: dict[str, Any]) -> tuple[int, float, Harness]:
    p, st = fx["params"], fx["strategy"]
    clients = sorted({r["agent"] or "sim" for r in fx["requests"]})
    h = Harness(
        tmp_path, cfg=parity_config(tmp_path, fx),
        knobs=MockKnobs(boot_s=0, price_usd_h=p["price_per_h"], offers=1, balance_usd=1e9),
        inference_knobs=MockInferenceKnobs(
            load_s=p["startup_s"], pp_tps=p["prefill_tps"] or 1e12, tg_tps=p["gen_tps"]),
        clients=tuple(clients), alive_check_s=3600, dup_sweep_s=3600,
    )  # fmt: skip
    t0 = h.clock.now()
    batching = st["min_batch"] > 1
    for i, r in enumerate(fx["requests"]):
        wait = r["max_wait_s"] if r["max_wait_s"] is not None else st["max_queue_wait_s"]
        h.arrive_at(t0 + r["t"], JobSpec(
            client_id=r["agent"] or "sim", body=f"sim-{i}".encode(),
            priority=_priority(r["prio"], batching), model_alias="m1", policy="BALANCED",
            data_class="private", route="cloud", pool="p1", route_reason="parity",
            max_queue_wait_s=wait, est_tokens_in=r["in"], max_tokens=r["out"],
        ))  # fmt: skip
    await h.run_until(t0 + fx["duration_s"] + 6 * 3600)
    launches = len(h.instances())
    return launches, h.ledger_total(), h


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
async def test_sim_parity(tmp_path: Path, path: Path) -> None:
    fx = json.loads(path.read_text(encoding="utf-8"))
    launches, usd, h = await run_fixture(tmp_path, fx)
    assert all(j["status"] == "completed" and j["route"] == "cloud" for j in h.jobs())
    assert all(r["state"] == "STOPPED" for r in h.instances())
    exp = fx["expected"]
    assert launches == exp["launches"], (launches, exp)
    assert abs(usd - exp["total_usd"]) <= 0.05 * exp["total_usd"], (usd, exp)


def test_fixtures_present_and_self_describing() -> None:
    traces = {p.stem.split("_")[0] for p in FIXTURES}
    assert traces == {"S1", "S2", "S3", "S4", "S5", "S6", "RF2", "RF4", "RF6"}
    assert len(FIXTURES) == 9 * 3 * 2
    head = json.loads(FIXTURES[0].read_text(encoding="utf-8"))["header"]
    assert len(head["sim_sha256"]) == 64 and "generate_sim_fixtures.py" in head["command"]
