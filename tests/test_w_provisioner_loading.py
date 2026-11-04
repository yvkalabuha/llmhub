"""E4/E5: pod material in create, need_public_ip, load progress and early E7 (T-370)."""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from harness import Harness, make_job
from llmhub.cloud_manager import inference as inference_mod
from llmhub.cloud_manager.inference import InferenceBackend, JobRunResult, JobRunSpec
from llmhub.cloud_manager.instance_env import derive_tls, make_material_fns
from llmhub.cloud_manager.load_status import LoadStatusSource
from llmhub.cloud_manager.provisioner import SLOW_CONFIRM_S
from llmhub.configuration.models import Config
from llmhub.provider_adapters.base import InstanceSpec, OfferQuery, RemoteResource
from llmhub.provider_adapters.mock import Fault
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
from w_fixtures import CA_PEMS, SECRETS, weights_cfg


class Spy:
    def __init__(self, h: Harness) -> None:
        self.specs: list[InstanceSpec] = []
        self.queries: list[OfferQuery] = []
        create, offers = h.provider.create, h.provider.list_offers

        async def spy_create(spec: InstanceSpec, label: str) -> RemoteResource:
            self.specs.append(spec)
            return await create(spec, label)

        async def spy_offers(q: OfferQuery) -> Any:
            self.queries.append(q)
            return await offers(q)

        h.provider.create = spy_create  # type: ignore[method-assign]
        h.provider.list_offers = spy_offers  # type: ignore[method-assign]


def make_h(
    tmp_path: Path, cfg: Config | None = None, knobs: MockInferenceKnobs | None = None
) -> Harness:
    cfg = cfg or weights_cfg(tmp_path)
    return Harness(tmp_path, cfg, inference_knobs=knobs,
                   materials=make_material_fns(cfg, SECRETS, CA_PEMS))  # fmt: skip


def reasons(h: Harness) -> list[str]:
    return [r for _, _, r in h.transitions()]


def _state_entered(h: Harness, state: str) -> float:
    row = h.conn.execute(
        "SELECT ts FROM transitions WHERE to_state=? ORDER BY id LIMIT 1", (state,)
    ).fetchone()
    return float(row[0])


@pytest.mark.parametrize("transport", ["direct", "proxy"])
async def test_create_gets_env_secret_env_and_fingerprint(tmp_path: Path, transport: str) -> None:
    h = make_h(tmp_path, weights_cfg(tmp_path, transport=transport))
    spy = Spy(h)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 120)
    inst = h.instances()[0]
    assert inst["state"] in ("READY", "BUSY", "IDLE")
    (spec,) = spy.specs
    assert spec.env["LLMHUB_OP_ID"] == inst["op_id"]
    assert spec.env["LLMHUB_TRANSPORT"] == transport
    assert {"LLMHUB_API_KEY", "LLMHUB_WEIGHTS_TOKEN"} <= set(spec.secret_env)
    assert all(q.need_public_ip == (transport == "direct") for q in spy.queries)
    if transport == "direct":
        fp = derive_tls(SECRETS.instance, inst["op_id"], inst["created_at"])[2]
        assert inst["tls_fingerprint"] == fp
        assert "LLMHUB_TLS_KEY_PEM" in spec.secret_env
    else:
        assert inst["tls_fingerprint"] is None
        assert "LLMHUB_TLS_KEY_PEM" not in spec.secret_env


async def test_pool_without_material_keeps_empty_env(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    spy = Spy(h)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 120)
    (spec,) = spy.specs
    assert dict(spec.env) == {} and dict(spec.secret_env) == {}
    assert not spy.queries[0].need_public_ip


async def test_repeated_create_same_label_same_material(tmp_path: Path) -> None:
    h = make_h(tmp_path, weights_cfg(tmp_path, transport="direct"))
    spy = Spy(h)
    h.provider.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED)
    t0 = h.clock.now()
    h.submit(make_job(0))
    await h.run_until(t0 + 400)
    assert len(spy.specs) == 2
    a, b = spy.specs
    assert dict(a.env) == dict(b.env) and dict(a.secret_env) == dict(b.secret_env)
    inst = h.instances()[0]
    assert (
        inst["tls_fingerprint"]
        == derive_tls(SECRETS.instance, inst["op_id"], inst["created_at"])[2]
    )


async def test_no_secrets_in_db_or_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    cfg = weights_cfg(tmp_path, transport="direct")
    h = make_h(tmp_path, cfg, MockInferenceKnobs(load_s=30))
    spy = Spy(h)
    h.provider.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 2000)
    secrets = [v for s in spy.specs for v in s.secret_env.values()]
    assert secrets
    pieces = set()
    for v in secrets:  # PEM: check every base64 line, not only the whole block
        pieces |= {ln for ln in v.splitlines() if len(ln) > 20 and "-----" not in ln}
        pieces.add(v)
    dump = (
        "\n".join(h.conn.iterdump())
        + caplog.text
        + h.provider.store.conn.execute("SELECT group_concat(label) FROM resources").fetchone()[0]
    )
    for p in pieces:
        assert p not in dump
    assert SECRETS.instance.decode() not in dump and SECRETS.weights.decode() not in dump


async def test_load_failed_gives_early_e7(tmp_path: Path) -> None:
    h = make_h(tmp_path, knobs=MockInferenceKnobs(load_s=900, load_fail="weights"))
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 200)
    assert "weights_failed:weights" in reasons(h)
    loading_at = _state_entered(h, "LOADING_MODEL")
    err_at = _state_entered(h, "ERROR")
    assert err_at - loading_at < 30  # long before loading_model_s (1200)
    inst = h.instances()[0]
    assert (
        h.conn.execute(
            "SELECT COUNT(*) FROM host_blacklist WHERE host_id=?", (inst["host_id"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        h.conn.execute("SELECT COUNT(*) FROM controller_state WHERE key LIKE 'load_%'").fetchone()[
            0
        ]
        == 0
    )


async def test_progress_is_written_then_cleared(tmp_path: Path) -> None:
    h = make_h(tmp_path, knobs=MockInferenceKnobs(load_s=60))
    h.submit(make_job(0))
    t0 = h.clock.now()
    await h.run_until(t0 + 80)  # boot 60 s, then loading 60 s
    inst = h.instances()[0]
    assert inst["state"] == "LOADING_MODEL"
    raw = h.conn.execute(
        "SELECT value FROM controller_state WHERE key=?", (f"load_progress:{inst['id']}",)
    ).fetchone()
    first = json.loads(raw[0])
    assert first["status"] == "loading" and first["phase"] == "downloading"
    await h.run_until(t0 + 90)
    second = json.loads(h.conn.execute(
        "SELECT value FROM controller_state WHERE key=?", (f"load_progress:{inst['id']}",)
    ).fetchone()[0])  # fmt: skip
    assert second["eta_s"] < first["eta_s"]  # updated on every poll
    await h.run_until(t0 + 300)
    assert h.instances()[0]["state"] in ("READY", "BUSY", "IDLE")
    assert (
        h.conn.execute("SELECT COUNT(*) FROM controller_state WHERE key LIKE 'load_%'").fetchone()[
            0
        ]
        == 0
    )


async def test_too_slow_without_fallback_early_e7(tmp_path: Path) -> None:
    knobs = MockInferenceKnobs(hang_load=True, load_eta_s=5000, load_sources_left=0)
    h = make_h(tmp_path, knobs=knobs)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 400)
    assert "weights_too_slow" in reasons(h)
    dt = _state_entered(h, "ERROR") - _state_entered(h, "LOADING_MODEL")
    assert 60 + SLOW_CONFIRM_S <= dt <= 60 + 2 * SLOW_CONFIRM_S  # >= 60 s, then 2 polls


async def test_slow_with_fallback_source_no_early_e7(tmp_path: Path) -> None:
    knobs = MockInferenceKnobs(hang_load=True, load_eta_s=5000, load_sources_left=1)
    h = make_h(tmp_path, knobs=knobs)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 1000)
    assert "weights_too_slow" not in reasons(h) and h.state_of() == "LOADING_MODEL"
    await h.run_until(h.clock.now() + 500)
    assert "loading_model_timeout" in reasons(h)


async def test_unknown_eta_no_early_e7(tmp_path: Path) -> None:
    knobs = MockInferenceKnobs(hang_load=True, load_eta_s=None, load_sources_left=0)
    h = make_h(tmp_path, knobs=knobs)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 1000)
    assert h.state_of() == "LOADING_MODEL" and "weights_too_slow" not in reasons(h)


async def test_slow_needs_two_polls_apart(tmp_path: Path) -> None:
    """Slowness must hold for SLOW_CONFIRM_S (30 s, fix round 1 F3); one fast poll resets it."""
    c = SLOW_CONFIRM_S
    knobs = MockInferenceKnobs(hang_load=True, load_eta_s=5000, load_sources_left=0)
    h = make_h(tmp_path, knobs=knobs)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 90)
    loading_at = _state_entered(h, "LOADING_MODEL")
    key = f"load_slow_since:{h.instances()[0]['id']}"

    def slow_since() -> float | None:
        row = h.conn.execute("SELECT value FROM controller_state WHERE key=?", (key,)).fetchone()
        return None if row is None else float(row[0])

    await h.run_until(loading_at + 60)
    await h.step()  # an extra step at the same time is not a second poll
    await h.step()
    assert slow_since() == loading_at + 60 and h.state_of() == "LOADING_MODEL"
    await h.run_until(loading_at + 60 + c - 6)  # still slow, but not long enough
    assert h.state_of() == "LOADING_MODEL"
    knobs.load_eta_s = 10  # one fast poll resets the streak
    await h.run_until(loading_at + 60 + c - 1)
    assert slow_since() is None and h.state_of() == "LOADING_MODEL"
    knobs.load_eta_s = 5000
    await h.run_until(loading_at + 60 + 2 * c - 2)
    assert slow_since() is not None and h.state_of() == "LOADING_MODEL"
    await h.run_until(loading_at + 60 + 2 * c + 10)
    assert "weights_too_slow" in reasons(h)


class _PlainBackend:
    """An InferenceBackend without load_status (old behaviour)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def ping(self, endpoint: str) -> bool:
        return bool(await self.inner.ping(endpoint))

    async def health(self, endpoint: str) -> bool:
        return bool(await self.inner.health(endpoint))

    async def test_request(self, endpoint: str) -> bool:
        return bool(await self.inner.test_request(endpoint))

    async def run(self, endpoint: str, job: JobRunSpec) -> JobRunResult:
        return await self.inner.run(endpoint, job)  # type: ignore[no-any-return]


async def test_backend_without_load_status_works_as_before(tmp_path: Path) -> None:
    h = make_h(tmp_path, knobs=MockInferenceKnobs(load_fail="weights"))
    plain = _PlainBackend(h.inference)
    assert not isinstance(plain, LoadStatusSource)
    assert isinstance(h.inference, LoadStatusSource)
    h.inference = plain  # type: ignore[assignment]
    h.controller = h._make_controller()
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 1500)
    assert "weights_failed:weights" not in reasons(h)
    assert [j["status"] for j in h.jobs()] == ["completed"]


def test_inference_protocol_frozen() -> None:
    methods = {
        n for n, v in vars(InferenceBackend).items() if inspect.isfunction(v) and n[0] != "_"
    }
    assert methods == {"ping", "health", "test_request", "run"}
    assert list(inspect.signature(InferenceBackend.run).parameters) == ["self", "endpoint", "job"]
    assert [f for f in JobRunSpec.__dataclass_fields__] == [
        "job_id", "model_alias", "tokens_in", "max_tokens", "body",
    ]  # fmt: skip
    assert [f for f in JobRunResult.__dataclass_fields__] == [
        "tokens_in", "tokens_out", "finish_reason", "body",
    ]  # fmt: skip
    assert "load_status" not in inspect.getsource(inference_mod)
