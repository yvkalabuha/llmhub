"""T-370 fix round 2: R2-1 (per-source causes, weights hold), R2-5 (NetworkLost counters),
R2-7 (tokens_out without usage)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from llmhub_infer.fetch import FetchError
from llmhub_infer.front import PodState

from harness import Harness, make_job
from i_fakes import FakeClock
from llmhub import controller as controller_mod
from llmhub.cloud_manager.http_inference import HttpInferenceBackend, assemble_sse
from llmhub.cloud_manager.inference import JobRunSpec
from llmhub.cloud_manager.load_status import MAX_CAUSES, parse_load_status
from llmhub.cloud_manager.provisioner import (
    WEIGHTS_HOLD_PREFIX,
    is_config_failure,
    set_weights_hold,
    weights_fingerprint,
    weights_hold_until,
)
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager import machine
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
from test_i_fetch import A_BASE, B_BASE, make, server, src
from test_i_front import AUTH, Upstream
from test_i_front import make as make_front
from test_w_provisioner_loading import make_h
from w_fixtures import weights_cfg

HOLD_S = 900.0
CONFIG_KNOBS = MockInferenceKnobs(load_fail="exhausted", load_causes=("sha_mismatch",))


# --- R2-1: classification ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("reason", "causes", "config"),
    [
        ("exhausted", ("sha_mismatch",), True),
        ("exhausted", ("http_4xx", "size_mismatch", "sha_mismatch"), True),
        ("sha_mismatch", (), True),
        ("http_4xx", (), True),
        ("exhausted", (), False),  # an old pod without causes -> host
        ("exhausted", ("network",), False),
        ("exhausted", ("slow",), False),
        ("exhausted", ("http_5xx", "sha_mismatch"), False),
        ("exhausted", ("http_4xx", "network"), False),
        ("exhausted", ("bogus",), False),
        ("disk", (), False),
        ("deadline", (), False),
        ("llama_exited", (), False),
    ],
)
def test_is_config_failure(reason: str, causes: tuple[str, ...], config: bool) -> None:
    assert is_config_failure(reason, causes) is config


def test_fetcher_exhausted_reports_per_source_causes(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", corrupt={"/w/op/d.gguf"})
    b = server(c, "/repo/resolve/rev/", status_for={"/repo/resolve/rev/d.gguf": 404})
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    with pytest.raises(FetchError) as e:
        env.run()
    assert e.value.reason == "exhausted"
    assert e.value.causes == ("http_4xx", "sha_mismatch")


async def test_front_failed_body_carries_causes() -> None:
    st = PodState()
    st.fail("exhausted", ("http_4xx", "sha_mismatch"))
    async with make_front(st, Upstream()) as cl:
        r = await cl.get("/health", headers=AUTH)
    assert r.status_code == 500
    assert r.json() == {"status": "failed", "reason": "exhausted",
                        "causes": ["http_4xx", "sha_mismatch"]}  # fmt: skip
    parsed = parse_load_status(r.json())
    assert parsed is not None and parsed.causes == ("http_4xx", "sha_mismatch")


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, ()),
        ("sha_mismatch", ()),  # not a list
        ([1, None, "", "network", "network"], ("network",)),
        (["x" * 100], ("x" * 32,)),
        ([f"c{i}" for i in range(100)], tuple(sorted(f"c{i}" for i in range(MAX_CAUSES)))),
    ],
)
def test_parse_causes_is_bounded_and_tolerant(raw: object, want: tuple[str, ...]) -> None:
    st = parse_load_status({"status": "failed", "reason": "exhausted", "causes": raw})
    assert st is not None and st.causes == want


# --- R2-1: hold ---------------------------------------------------------------------------------
def _cfg(tmp_path: Path) -> Config:
    cfg = weights_cfg(tmp_path)
    return cfg.model_copy(
        update={"retry": cfg.retry.model_copy(update={"host_blacklist_s": HOLD_S})}
    )


def _launches(h: Harness) -> int:
    return int(h.conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0])


async def _wave(h: Harness, n: int, dt: float = 120.0) -> None:
    h.submit(make_job(n))
    await h.run_until(h.clock.now() + dt)


async def test_config_error_holds_pool_no_more_paid_launches(tmp_path: Path) -> None:
    h = make_h(tmp_path, _cfg(tmp_path), knobs=CONFIG_KNOBS)
    await _wave(h, 0)
    assert _launches(h) == 1
    assert h.conn.execute("SELECT COUNT(*) FROM host_blacklist").fetchone()[0] == 0
    (inc,) = h.incidents("weights_config")
    assert inc["severity"] == "critical"
    details = json.loads(inc["details_json"])
    assert details["causes"] == ["sha_mismatch"] and details["pool"] == "p1"
    assert weights_hold_until(h.conn, "p1", h.clock.now(), h.cfg) == pytest.approx(
        details["hold_until"]
    )
    for n in range(1, 4):  # every later wave goes local at once, no launch
        await _wave(h, n, 60.0)
    assert _launches(h) == 1
    jobs = h.jobs()
    assert len(jobs) == 4 and all(j["route"] == "local:fallback" for j in jobs)


async def test_host_fault_still_blacklists_and_does_not_hold(tmp_path: Path) -> None:
    knobs = MockInferenceKnobs(load_fail="exhausted", load_causes=("network", "sha_mismatch"))
    h = make_h(tmp_path, _cfg(tmp_path), knobs=knobs)
    await _wave(h, 0)
    assert h.conn.execute("SELECT COUNT(*) FROM host_blacklist").fetchone()[0] >= 1
    assert weights_hold_until(h.conn, "p1", h.clock.now(), h.cfg) is None
    assert not h.incidents("weights_config")


async def test_hold_expires_after_host_blacklist_s(tmp_path: Path) -> None:
    h = make_h(tmp_path, _cfg(tmp_path), knobs=CONFIG_KNOBS)
    await _wave(h, 0)
    until = weights_hold_until(h.conn, "p1", h.clock.now(), h.cfg)
    assert until is not None
    await _wave(h, 1, 60.0)
    assert _launches(h) == 1
    await h.run_until(until + 1)
    await _wave(h, 2)
    assert _launches(h) == 2  # one more try after the hold; it fails again -> a new hold
    assert weights_hold_until(h.conn, "p1", h.clock.now(), h.cfg) is not None


async def test_admin_clear_lifts_hold(tmp_path: Path) -> None:
    h = make_h(tmp_path, _cfg(tmp_path), knobs=CONFIG_KNOBS)
    await _wave(h, 0)
    h.inference.knobs = MockInferenceKnobs()  # the admin fixed the sources
    assert h.controller.clear_weights_hold("p1") is True
    assert h.controller.clear_weights_hold("p1") is False
    await _wave(h, 1, 300.0)
    assert _launches(h) == 2
    assert any(j["status"] == "completed" for j in h.jobs())


async def test_hold_survives_controller_restart(tmp_path: Path) -> None:
    h = make_h(tmp_path, _cfg(tmp_path), knobs=CONFIG_KNOBS)
    await _wave(h, 0)
    h.restart()
    await _wave(h, 1, 60.0)
    assert _launches(h) == 1


async def test_prewarm_only_while_held_does_not_launch(tmp_path: Path) -> None:
    h = make_h(tmp_path, _cfg(tmp_path), knobs=CONFIG_KNOBS)
    set_weights_hold(h.conn, "p1", h.clock.now(), h.cfg, "weights_failed:exhausted")
    from llmhub.lifecycle_manager import leases

    leases.create_explicit(h.conn, holder="t", client_id=None, pool="p1", ttl_s=600,
                           now=h.clock.now(), prewarm=True, cfg=h.cfg.leases)  # fmt: skip
    await h.run_until(h.clock.now() + 120)
    assert _launches(h) == 0
    h.controller.clear_weights_hold("p1")  # control: the same prewarm launches without a hold
    await h.run_until(h.clock.now() + 60)
    assert _launches(h) == 1


def test_config_change_or_expiry_voids_hold(tmp_path: Path) -> None:
    h = make_h(tmp_path, _cfg(tmp_path))
    now = h.clock.now()
    until = set_weights_hold(h.conn, "p1", now, h.cfg, "weights_failed:sha_mismatch")
    assert until == now + HOLD_S
    assert weights_hold_until(h.conn, "p1", now + 10, h.cfg) == until
    assert weights_hold_until(h.conn, "p1", until, h.cfg) is None  # expired
    sets = h.cfg.weights.sets
    files = [f.model_copy(update={"sha256": "0" * 64}) for f in sets["set1"].files]
    new_set = sets["set1"].model_copy(update={"files": files})
    cfg2 = h.cfg.model_copy(
        update={"weights": h.cfg.weights.model_copy(update={"sets": {"set1": new_set}})}
    )
    assert weights_fingerprint(cfg2, "p1") != weights_fingerprint(h.cfg, "p1")
    assert weights_hold_until(h.conn, "p1", now + 10, cfg2) is None  # fixed config -> no hold


def test_unreadable_hold_key_is_ignored(tmp_path: Path) -> None:
    h = make_h(tmp_path, _cfg(tmp_path))
    with h.conn:
        h.conn.execute(
            "INSERT INTO controller_state(key, value, updated_at) VALUES (?,?,0)",
            (f"{WEIGHTS_HOLD_PREFIX}p1", "{not json"),
        )
    assert weights_hold_until(h.conn, "p1", h.clock.now(), h.cfg) is None


# --- R2-5: NetworkLost counters -------------------------------------------------------------------
async def test_netlost_counters_pruned_for_dead_instances_and_finished_jobs(
    tmp_path: Path,
) -> None:
    h = make_h(tmp_path, _cfg(tmp_path))
    await _wave(h, 0, 300.0)
    (inst,) = h.instances()
    (job,) = h.jobs()
    assert inst["state"] in ("READY", "IDLE") and job["status"] == "completed"
    ctl = h.controller
    ctl._netlost_inst.update({inst["id"]: 1, "gone": 2})
    ctl._netlost_job.update({(job["id"], inst["id"]): 1, ("j-x", "gone"): 1})
    ctl._prune_netlost()
    assert ctl._netlost_inst == {inst["id"]: 1}  # still serving: kept
    assert ctl._netlost_job == {}  # completed job / unknown instance: dropped
    machine.mark_lost(h.conn, inst["id"], h.clock.now(), "test", cfg=h.cfg)
    await h.step()
    assert ctl._netlost_inst == {}


async def test_netlost_counters_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_h(tmp_path, _cfg(tmp_path))
    await _wave(h, 0, 300.0)
    (inst,) = h.instances()
    monkeypatch.setattr(controller_mod, "NETWORK_LOST_MAX_ENTRIES", 0)
    h.controller._netlost_inst[inst["id"]] = 1
    h.controller._prune_netlost()
    assert h.controller._netlost_inst == {}


# --- R2-7: tokens_out without usage ---------------------------------------------------------------
def _spec(max_tokens: int = 1000) -> JobRunSpec:
    return JobRunSpec(job_id="j", model_alias="m1", tokens_in=50, max_tokens=max_tokens, body=b"")


def test_stream_cut_before_usage_estimates_tokens_out() -> None:
    def ev(obj: dict[str, object]) -> bytes:
        return b"data: " + json.dumps(obj).encode() + b"\n\n"

    raw = (
        ev({"choices": [{"index": 0, "delta": {"content": "a" * 30}}]})
        + ev({"choices": [{"index": 0, "delta": {"content": "b" * 10},
                           "finish_reason": "stop"}]})
    )  # fmt: skip  (no usage chunk, no [DONE])
    data, complete = assemble_sse(raw)
    assert complete and data is not None and "usage" not in data
    assert HttpInferenceBackend._tokens(data, None, _spec()) == (50, 10)


def test_estimate_counts_tool_calls_and_respects_max_tokens() -> None:
    data = {"choices": [{"message": {"content": "", "tool_calls": [
        {"function": {"name": "f", "arguments": "x" * 39}}]}}]}  # fmt: skip
    assert HttpInferenceBackend._tokens(data, None, _spec()) == (50, 10)
    assert HttpInferenceBackend._tokens(data, None, _spec(max_tokens=3)) == (50, 3)
    assert HttpInferenceBackend._tokens({"choices": "junk"}, None, _spec()) == (50, 0)


def test_usage_still_wins_over_estimate() -> None:
    data = {"choices": [{"message": {"content": "a" * 400}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7}}  # fmt: skip
    assert HttpInferenceBackend._tokens(data, None, _spec()) == (5, 7)
