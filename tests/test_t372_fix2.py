"""T-372 fix round 2 (review cycle 3): R3-1 (guard on Python 3.10), R3-2 (DELETE 404 is
GET-verified), R3-3 (Community offer from `communityPrice`/`communityCloud`, the real GraphQL
answers in `tests/fixtures/runpod/graphql/`), R3-5 ("400 + not listed" is gone only for a pod
known longer than `list_staleness_s`), R3-6 (a malformed foreign pod blinds nothing).
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

from llmhub.clock import FakeClock
from llmhub.provider_adapters.base import (
    CreatedResourceRejected,
    DestroyResult,
    OfferQuery,
    ProviderError,
    UnavailableReason,
)
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.runpod import RunpodAdapter
from llmhub.provider_adapters.runpod_api import RunpodApi, RunpodParseError
from runpod_fake import FAKE_KEY, GPU_ID, FakeRunpod, make_adapter, make_spec, runpod_cfg
from t372_scripts import SCRIPTS, load

g = load("runpod_guard")
ROOT = Path(__file__).resolve().parent.parent
GQL = ROOT / "tests" / "fixtures" / "runpod" / "graphql"
UNIT_PYTHON = Path("/usr/bin/python3")  # ExecStart of deploy/workstation/llmhub-runpod-guard.service
SECRET = b"test-secret-0123456789abcdef-test-secret"
OURS = "llmhub-0123456789ab-01234567"


def _label() -> str:
    return make_label(new_op_id(), SECRET)


def _unit_python() -> Path:
    if not UNIT_PYTHON.exists():
        pytest.skip("no /usr/bin/python3")
    return UNIT_PYTHON


# ------------------------------------------------------------------ R3-1: the unit interpreter
def _run_unit(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "h" / "run.py"
    harness.parent.mkdir(exist_ok=True)
    harness.write_text(textwrap.dedent(f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("g", {str(SCRIPTS / "runpod_guard.py")!r})
        g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        print("python", sys.version_info[:2])
    """) + textwrap.dedent(body))
    return subprocess.run([str(_unit_python()), "-I", str(harness)], capture_output=True,
                          text=True, timeout=60, check=False)  # fmt: skip


def test_guard_time_formats_under_the_unit_interpreter(tmp_path: Path) -> None:
    """3.10 `fromisoformat` takes neither `Z` nor 2/9 fraction digits: `_time` normalizes."""
    out = _run_unit(tmp_path, """
        for s in ("2026-11-04T10:00:00Z", "2026-11-04T10:00:00.12Z", "2026-11-04T10:00:00.123Z",
                  "2026-11-04T10:00:00.123456789Z", "2026-11-04 10:00:00.1 +0000 UTC",
                  "2026-11-04 10:00:00 +0000 UTC", "2026-11-04T10:00:00", "2026-11-04T13:00:00.5+03:00"):
            print(repr(s), int(g._time(s, "t")))
    """)
    assert out.returncode == 0, out.stderr[-600:]
    stamps = {int(line.rsplit(" ", 1)[1]) for line in out.stdout.splitlines()[1:]}
    assert stamps == {1_793_786_400}, out.stdout  # 2026-11-04T10:00:00Z


def test_guard_cli_watch_and_record_under_the_unit_interpreter(tmp_path: Path) -> None:
    """The whole `main` (key files, before.json, state dir, record) runs on 3.10."""
    (tmp_path / "rec").mkdir()
    out = _run_unit(tmp_path, f"""
        import json, os, pathlib
        tmp = pathlib.Path({str(tmp_path)!r})
        pod = {{"id": "p1", "name": {OURS!r}, "desiredStatus": "RUNNING", "costPerHr": "0.25",
               "lastStartedAt": "2026-11-04T10:00:00.12Z"}}
        def fake(method, url, key, body=None):
            if "graphql" in url:
                return {{"data": {{"myself": {{"clientBalance": 19.0, "currentSpendPerHr": 0.9}}}}}}
            if url.endswith("/pods") or "/pods?" in url:
                return [pod, {{"id": "f1", "costPerHr": "odd"}}]
            if method == "DELETE":
                return None
            return []
        g.http_json = fake
        for name in ("ro", "rw"):
            (tmp / name).write_text("rpa_x\\n"); os.chmod(tmp / name, 0o600)
        (tmp / "before.json").write_text(json.dumps({{"taken_at": g.NOW(), "balance": 20.0,
                                                      "pods": []}}))
        print("watch", g.main(["watch", "--once", "--auto-destroy", "--key-file", str(tmp / "ro"),
                               "--rw-key-file", str(tmp / "rw"), "--before", str(tmp / "before.json"),
                               "--state-dir", str(tmp / "st")]))
        print("record", g.main(["record", "--key-file", str(tmp / "ro"), "--dir", str(tmp / "rec")]))
    """)
    assert out.returncode == 0, out.stderr[-600:]
    assert "destroyed pod p1" in out.stdout and "watch 6" in out.stdout, out.stdout
    assert "record 0" in out.stdout, out.stdout + out.stderr[-600:]
    assert (tmp_path / "st" / "actions.log").read_text().count("destroy pod p1") == 1


# ------------------------------------------------------------------ R3-2
def test_guard_delete_404_then_get_404_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(method: str, url: str, key: str, body: Any = None) -> Any:
        calls.append(method)
        raise g.GuardError(7, f"{method} -> HTTP 404", 404)

    monkeypatch.setattr(g, "http_json", fake)
    assert g.delete_pod("p1", "rw") == "gone"
    assert calls == ["DELETE", "GET"]


# ------------------------------------------------------------------ R3-6: guard
def _guard_api(pods: list[Any], spend: float = 0.0) -> tuple[Any, list[str]]:
    deleted: list[str] = []

    def fake(method: str, url: str, key: str, body: Any = None) -> Any:
        if "graphql" in url:
            return {"data": {"myself": {"clientBalance": 19.0, "currentSpendPerHr": spend}}}
        if method == "DELETE":
            deleted.append(url.rsplit("/", 1)[1])
            return None
        if url.endswith("/pods"):
            return pods
        return []

    return fake, deleted


FOREIGN_MALFORMED = [
    {"id": "f1", "desiredStatus": "RUNNING"},  # no name
    {"id": "f2", "name": None, "desiredStatus": "RUNNING"},
    {"id": "f3", "name": "someone", "costPerHr": {"x": 1}},  # no desiredStatus, odd cost
    "not-an-object",
]


def test_guard_foreign_malformed_pods_do_not_blind_it(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path, capsys: Any) -> None:  # fmt: skip
    ours = {"id": "p1", "name": OURS, "desiredStatus": "RUNNING", "costPerHr": 0.25}
    fake, deleted = _guard_api([*FOREIGN_MALFORMED, ours], spend=0.9)
    monkeypatch.setattr(g, "http_json", fake)
    before = {"taken_at": 0.0, "balance": 20.0, "pods": []}
    assert g.watch_pass("ro", "rw", before, tmp_path, until=1e12) == 6
    assert deleted == ["p1"]
    out = capsys.readouterr().out
    assert "foreign pod f1" in out and "STOP: parse error" in out  # flagged, after the deletes
    fake2, deleted2 = _guard_api(FOREIGN_MALFORMED)
    monkeypatch.setattr(g, "http_json", fake2)
    assert g.watch_pass("ro", "rw", before, tmp_path, until=1e12) == 4  # flagged as STOP
    assert deleted2 == []


def test_guard_our_label_stays_strict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake, deleted = _guard_api([{"id": "p1", "name": OURS}], spend=0.9)  # no desiredStatus
    monkeypatch.setattr(g, "http_json", fake)
    with pytest.raises(g.GuardError) as ei:
        g.watch_pass("ro", "rw", {"taken_at": 0.0, "balance": 20.0, "pods": []}, tmp_path,
                     until=1e12)  # fmt: skip
    assert ei.value.code == 4 and deleted == []


def test_destroy_leftovers_skips_foreign_malformed(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:  # fmt: skip
    ours = {"id": "p1", "name": OURS, "desiredStatus": "EXITED"}
    fake, deleted = _guard_api([*FOREIGN_MALFORMED, ours])
    monkeypatch.setattr(g, "http_json", fake)
    (tmp_path / "rw").write_text("rpa_x\n")
    (tmp_path / "rw").chmod(0o600)
    (tmp_path / "b.json").write_text(json.dumps({"taken_at": 0.0, "balance": 20.0, "pods": []}))
    code = g.main(["destroy-leftovers", "--key-file", str(tmp_path / "rw"), "--before",
                   str(tmp_path / "b.json"), "--yes"])  # fmt: skip
    assert code == 0 and deleted == ["p1"]


# ------------------------------------------------------------------ R3-3: real GraphQL answers
def _fixture(name: str) -> dict[str, Any]:
    return json.loads((GQL / name).read_text())["answer"]


ALL_TYPES = {x["id"]: x for x in _fixture("gpu_types_secure_false.json")["data"]["gpuTypes"]}
LOWEST_NO_SECURE = _fixture("gpu_type_3090_lowest_no_secure.json")["data"]["gpuTypes"][0]


def _real_answer(query: str) -> dict[str, Any]:
    """Answer a `gpuTypes(input: {id})` query from the recorded L1 data: the type fields from
    the full list; `lowestPrice` null for `secureCloud: false` (recorded), the reported
    3090 value without it. `displayName` was not asked in the full list: id stands in."""
    if "myself" in query:
        return {"data": {"myself": _fixture("myself_and_3090_secure_false.json")
                         ["data"]["myself"]}}  # fmt: skip
    m = re.search(r'id: "([^"]+)"', query)
    assert m is not None
    t = ALL_TYPES.get(m.group(1))
    if t is None:
        return {"data": {"gpuTypes": []}}
    item = {**t, "displayName": t["id"]}
    low = re.search(r"lowestPrice\(input: *\{([^}]*)\}\)", query)
    if low is None:
        item.pop("lowestPrice")
    elif "secureCloud" not in low.group(1):
        # only the 3090 value was reported; others: unknown stock (a hint only)
        item["lowestPrice"] = (LOWEST_NO_SECURE["lowestPrice"] if t["id"] == LOWEST_NO_SECURE["id"]
                               else {"uninterruptablePrice": None, "stockStatus": None})
    return {"data": {"gpuTypes": [item]}}


class _Real:
    """GraphQL from the recorded answers; REST: `GET /pods` -> [], POST -> `post` answer."""

    def __init__(self, post: httpx.Response | None = None) -> None:
        self.queries: list[str] = []
        self.posts = 0
        self.post = post or httpx.Response(599, json={})

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.runpod.io":
            q = json.loads(request.content)["query"]
            self.queries.append(q)
            return httpx.Response(200, json=_real_answer(q))
        if request.method == "GET" and request.url.path.endswith("/pods"):
            return httpx.Response(200, json=[])
        if request.method == "POST":
            self.posts += 1
            return self.post
        return httpx.Response(599, json={})

    def adapter(self, **cfg: Any) -> RunpodAdapter:
        api = RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(self.handle))
        return RunpodAdapter(api, runpod_cfg(**cfg), FakeClock(), disk_gb_hint=40)


GPUS = {"RTX3090": "NVIDIA GeForce RTX 3090", "RTX3090TI": "NVIDIA GeForce RTX 3090 Ti",
        "RTX4090": "NVIDIA GeForce RTX 4090", "A5000": "NVIDIA RTX A5000",
        "A6000": "NVIDIA RTX A6000", "A40": "NVIDIA A40"}  # fmt: skip


@pytest.mark.parametrize(("gpu", "price"), [("RTX3090", 0.22), ("RTX3090TI", 0.27),
                                            ("RTX4090", 0.34), ("A5000", 0.16),
                                            ("A6000", 0.33), ("A40", None)])  # fmt: skip
async def test_offers_from_recorded_l1_answer(gpu: str, price: float | None) -> None:
    """The coordinator's L1 prices; A40 has `communityCloud: false` -> no offer."""
    real = _Real()
    offers = await real.adapter(gpu_type_ids=GPUS).list_offers(
        OfferQuery(gpu=gpu, min_vram_gb=24, max_price_usd_h=0.5, exclude_hosts=frozenset()))
    assert [o.price_usd_h for o in offers] == ([] if price is None else [price])
    (q,) = real.queries
    assert "communityPrice communityCloud" in q and "secureCloud" not in q
    assert "uninterruptablePrice" not in q  # may be a Secure price: never a ceiling input


def test_recorded_answers_confirm_the_secure_false_nulls() -> None:
    """Why R3-3: the old query (`secureCloud: false`) gave nulls for the types we use."""
    for gid in GPUS.values():
        assert ALL_TYPES[gid]["lowestPrice"]["uninterruptablePrice"] is None, gid
    me = _fixture("myself_and_3090_secure_false.json")["data"]
    assert me["myself"]["spendLimit"] == 80 and me["gpuTypes"][0]["lowestPrice"] == {
        "uninterruptablePrice": None, "stockStatus": None}


async def test_community_price_above_ceiling_refused_before_post() -> None:
    real = _Real()
    with pytest.raises(ProviderError) as ei:  # 4090: 0.34 > spec 0.30
        await real.adapter(gpu_type_ids=GPUS).create(
            make_spec(offer_id=GPUS["RTX4090"]), _label())
    assert ei.value.err.reason == UnavailableReason.DEPLOYMENT_ERROR and real.posts == 0
    assert "above ceiling" in ei.value.err.message


async def test_secure_lowest_price_is_never_the_ceiling_input() -> None:
    """`lowestPrice` (no secureCloud) may quote a cheaper/other price: Community decides."""
    fake = FakeRunpod(FakeClock(), price=0.40, secure_price=0.10)
    with pytest.raises(ProviderError) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert "above ceiling" in ei.value.err.message and fake.posts() == 0


async def test_numeric_string_community_price_and_null_stock_still_offer() -> None:
    fake = FakeRunpod(FakeClock(), price_as_string=True, stock=None)
    a = make_adapter(fake)
    (o,) = await a.list_offers(OfferQuery("RTX3090", 24, 0.3, frozenset()))
    assert o.price_usd_h == 0.22
    res = await a.create(make_spec(), _label())
    assert fake.posts() == 1 and res.price_usd_h == pytest.approx(0.225)


@pytest.mark.parametrize("bad", ["cheap", {"x": 1}, True])
async def test_bad_community_price_is_a_parse_error(bad: Any) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"gpuTypes": [{
            "id": GPU_ID, "displayName": "RTX 3090", "memoryInGb": 24, "communityPrice": bad,
            "communityCloud": True, "lowestPrice": None}]}})  # fmt: skip

    api = RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(handle))
    with pytest.raises(RunpodParseError):
        await api.gpu_types([GPU_ID])


async def test_no_capacity_on_post_is_a_definite_refusal() -> None:
    """With stockStatus only a hint, capacity is decided by the POST: `no_capacity`
    -> GPU_UNAVAILABLE, not an unknown outcome, exactly one POST per CUDA version (L2b)."""
    real = _Real(httpx.Response(500, text="There are no longer any instances available with "
                                          "the requested specifications."))  # fmt: skip
    with pytest.raises(ProviderError) as ei:
        await real.adapter().create(make_spec(), _label())
    assert ei.value.err.reason == UnavailableReason.GPU_UNAVAILABLE
    assert not ei.value.err.outcome_unknown and real.posts == 3


async def test_cost_after_create_still_checked() -> None:
    fake = FakeRunpod(FakeClock(), cost_override=0.60)
    with pytest.raises(CreatedResourceRejected) as ei:
        await make_adapter(fake).create(make_spec(), _label())
    assert ei.value.what.startswith("price") and fake.posts() == 1


def test_fake_answers_secure_false_with_nulls_like_the_real_api() -> None:
    fake = FakeRunpod(FakeClock())
    q = ("query { gpuTypes(input: {id: " + json.dumps(GPU_ID) + "}) { id lowestPrice(input: "
         "{gpuCount: 1, secureCloud: false}) { uninterruptablePrice stockStatus } } }")
    item = fake._gpu_type(q)  # pyright: ignore[reportPrivateUsage]
    assert item["lowestPrice"] == {"uninterruptablePrice": None, "stockStatus": None}


# ------------------------------------------------------------------ R3-5
def _unlisted_handler(listed: list[dict[str, Any]]) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/pods":
            return httpx.Response(200, json=listed)
        return httpx.Response(400, json={"error": "bad"})

    return handle


async def test_unlisted_young_pod_is_not_gone_until_the_list_had_time() -> None:
    clock = FakeClock()
    api = RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(_unlisted_handler([])))
    a = RunpodAdapter(api, runpod_cfg(list_staleness_s=30), clock, disk_gb_hint=40)
    for call in (a.destroy, a.get):
        with pytest.raises(ProviderError) as ei:
            await call("pod0001abc")
        assert ei.value.err.retryable and ei.value.err.outcome_unknown
    clock.advance_to(clock.now() + 29)
    with pytest.raises(ProviderError):
        await a.destroy("pod0001abc")
    clock.advance_to(clock.now() + 1)
    assert await a.destroy("pod0001abc") == DestroyResult.ALREADY_GONE
    assert await a.get("pod0001abc") is None


async def test_created_pod_first_known_at_create() -> None:
    """DELETE 400 -> GET 400 -> not listed right after create: unknown, not ALREADY_GONE
    (a just-rejected pod would otherwise get no `created_resource_destroy_failed`)."""
    clock = FakeClock()
    fake = FakeRunpod(clock, visible_after_s=60, cost_override=0.6)
    a = make_adapter(fake, list_staleness_s=30)
    with pytest.raises(CreatedResourceRejected) as ei:
        await a.create(make_spec(), _label())
    ref = ei.value.resource.ref
    clock.advance_to(clock.now() + 10)
    fake.inject("DELETE", "/pods/", "status", status=400)
    fake.inject("GET", "/pods/", "status", status=400)
    with pytest.raises(ProviderError) as e2:
        await a.destroy(ref)
    assert e2.value.err.outcome_unknown


async def test_unlisted_with_zero_staleness_is_gone_at_once() -> None:
    api = RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(_unlisted_handler([])))
    a = RunpodAdapter(api, runpod_cfg(list_staleness_s=0), FakeClock(), disk_gb_hint=40)
    assert await a.destroy("pod0001abc") == DestroyResult.ALREADY_GONE


# ------------------------------------------------------------------ R3-6: adapter
async def test_adapter_list_skips_malformed_foreign_pods() -> None:
    ours = {"id": "p1", "name": OURS, "desiredStatus": "RUNNING", "costPerHr": 0.25}
    pods = [{"id": "f1", "desiredStatus": "RUNNING"}, {"id": "f2", "name": "x",
            "desiredStatus": "RUNNING", "costPerHr": "cheap"}, "junk", ours,
            {"id": "f3", "name": "fine", "desiredStatus": "RUNNING", "costPerHr": 0.1}]  # fmt: skip

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pods if request.url.path == "/v1/pods" else [])

    api = RunpodApi(lambda: FAKE_KEY, transport=httpx.MockTransport(handle))
    assert [p.id for p in await api.list_pods()] == ["p1", "f3"]
    a = RunpodAdapter(api, runpod_cfg(), FakeClock(), disk_gb_hint=40)
    assert [r.ref for r in await a.list_owned()] == ["p1"]
    pods.append({"id": "p2", "name": "llmhub-aaaaaaaaaaaa-bbbbbbbb", "desiredStatus": "RUNNING",
                 "costPerHr": "cheap"})  # fmt: skip
    with pytest.raises(RunpodParseError):  # our label: strict
        await api.list_pods()
