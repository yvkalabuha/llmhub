import hashlib
import logging
import os
import random
from collections import namedtuple
from pathlib import Path

import httpx
import pytest
from llmhub_infer import fetch as fetch_mod
from llmhub_infer.fetch import Fetcher, FetchError, Progress
from llmhub_infer.plan import parse_plan
from llmhub_infer.progress import read_progress, write_progress

from i_fakes import FakeClock, FakeWeightsServer, TlsServer, make_plan_json
from tls_certs import make_self_signed

_rnd = random.Random(7)
MODEL = _rnd.randbytes(200 * 1024)
DRAFT = _rnd.randbytes(64 * 1024)
FILES = {"model": MODEL, "draft": DRAFT}
TOKEN = "tok-SECRET-123"
HF = "hf-SECRET-456"

A_BASE = "https://weights.test:9443/w/op/"
B_BASE = "https://huggingface.co/repo/resolve/rev/"
PATHS = {"model": "m.gguf", "draft": "d.gguf"}


def src(name: str, base: str, **kw: object) -> dict:
    return {"name": name, "base_url": base, "paths": PATHS, **kw}


def served(prefix: str, files: dict[str, bytes] = FILES) -> dict[str, bytes]:
    return {prefix + PATHS[k]: v for k, v in files.items()}


Env = namedtuple("Env", "clock dest events run")


def make(
    tmp_path: Path,
    servers: dict[str, FakeWeightsServer],
    sources: list[dict],
    clock: FakeClock,
    *,
    secrets: dict[str, str] | None = None,
    files: dict[str, bytes] = FILES,
    deadline: float | None = None,
    chunk: int = 32 * 1024,
    **plan_over: object,
) -> Env:
    plan = parse_plan(make_plan_json(files, sources, deadline_unix=deadline, **plan_over))
    events: list[Progress] = []
    by_name = {s.name: servers[s.name] for s in plan.sources}
    f = Fetcher(
        plan,
        tmp_path,
        secrets or {},
        events.append,
        clock,
        lambda s: by_name[s.name],
        wall=clock,
        sleep=clock.sleep,
        chunk_size=chunk,
        disk_margin=0,
    )
    return Env(clock, tmp_path, events, f.run)


def server(clock: FakeClock, prefix: str, files: dict[str, bytes] = FILES, **kw: object):
    return FakeWeightsServer(served(prefix, files), clock, **kw)  # type: ignore[arg-type]


def check_done(env: Env, out: dict[str, Path]) -> None:
    for k, data in FILES.items():
        assert out[k] == env.dest / f"{k}.gguf"
        assert out[k].read_bytes() == data
        assert (env.dest / f"{k}.gguf.sha256").read_text() == hashlib.sha256(data).hexdigest()
        assert not (env.dest / f"{k}.gguf.part").exists()


def test_01_happy_path(tmp_path: Path) -> None:
    c = FakeClock()
    s = server(c, "/w/op/")
    env = make(tmp_path, {"a": s}, [src("a", A_BASE)], c)
    out = env.run()
    check_done(env, out)
    assert env.events[-1].phase == "done"
    assert next(r.path for r in s.log) == "/w/op/d.gguf"  # draft first


def test_02_range_resume_after_drop(tmp_path: Path) -> None:
    c = FakeClock()
    s = server(c, "/w/op/", drop_after_bytes=50_000)
    env = make(tmp_path, {"a": s}, [src("a", A_BASE)], c)
    check_done(env, env.run())
    reqs = s.requests_for("/w/op/d.gguf")
    assert reqs[0].range is None and reqs[1].range == "bytes=50000-"


def test_03_range_ignored_restarts(tmp_path: Path) -> None:
    c = FakeClock()
    (tmp_path / "model.gguf.part").write_bytes(MODEL[:1000])
    s = server(c, "/w/op/", ignore_range=True)
    env = make(tmp_path, {"a": s}, [src("a", A_BASE)], c)
    check_done(env, env.run())


def test_04_416_when_part_larger(tmp_path: Path) -> None:
    c = FakeClock()
    (tmp_path / "draft.gguf.part").write_bytes(DRAFT + b"extra")
    s = server(c, "/w/op/")
    env = make(tmp_path, {"a": s}, [src("a", A_BASE)], c)
    check_done(env, env.run())


def test_04b_reactive_416(tmp_path: Path) -> None:
    c = FakeClock()
    (tmp_path / "draft.gguf.part").write_bytes(DRAFT[:100])
    s = server(c, "/w/op/")
    # server claims the range is unsatisfiable once -> truncate and restart
    orig = s.handle_request
    state = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("d.gguf") and req.headers.get("range") and not state["n"]:
            state["n"] = 1
            return httpx.Response(416)
        return orig(req)

    s.handle_request = h  # type: ignore[method-assign]
    env = make(tmp_path, {"a": s}, [src("a", A_BASE)], c)
    check_done(env, env.run())


def test_05_failover_on_5xx_and_part_continues(tmp_path: Path) -> None:
    c = FakeClock()
    (tmp_path / "draft.gguf.part").write_bytes(DRAFT[:3000])
    a = server(c, "/w/op/", status_for={"/w/op/d.gguf": 503})
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert len(a.requests_for("/w/op/d.gguf")) == 3  # retries + 1
    assert b.requests_for("/repo/resolve/rev/d.gguf")[0].range == "bytes=3000-"
    assert env.events[-1].failovers == [{"source": "a", "file": "draft", "reason": "http_5xx"}]


def test_06_403_goes_next_without_retry(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", status_for={"/w/op/d.gguf": 403})
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert len(a.requests_for("/w/op/d.gguf")) == 1
    assert env.events[-1].failovers[0]["reason"] == "http_4xx"


def test_07_failover_on_speed(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", throttle_bps=20 * 1024)
    b = server(c, "/repo/resolve/rev/")
    env = make(
        tmp_path,
        {"a": a, "b": b},
        [src("a", A_BASE, min_speed_mb_s=0.2), src("b", B_BASE)],
        c,
    )
    t0 = c()
    check_done(env, env.run())
    assert c() - t0 <= 2.0
    assert env.events[-1].failovers[0]["reason"] == "slow"
    assert b.requests_for("/repo/resolve/rev/d.gguf")


def test_08_last_source_slow_not_aborted(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", throttle_bps=20 * 1024)
    env = make(tmp_path, {"a": a}, [src("a", A_BASE, min_speed_mb_s=0.2)], c)
    check_done(env, env.run())
    assert env.events[-1].failovers == []


def test_09_sha_mismatch_then_next(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", corrupt={"/w/op/d.gguf"})
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert env.events[-1].failovers[0]["reason"] == "sha_mismatch"
    assert b.requests_for("/repo/resolve/rev/d.gguf")[0].range is None  # .part was deleted


def test_09b_all_corrupt_exhausted(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", corrupt={"/w/op/d.gguf"})
    b = server(c, "/repo/resolve/rev/", corrupt={"/repo/resolve/rev/d.gguf"})
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    with pytest.raises(FetchError) as e:
        env.run()
    assert e.value.reason == "exhausted"
    assert env.events[-1].phase == "failed" and env.events[-1].error == "exhausted"
    assert not (tmp_path / "draft.gguf.part").exists()


def test_10_size_mismatch_smaller_next_source(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", files={"model": MODEL, "draft": DRAFT[:-10]})
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert env.events[-1].failovers[0]["reason"] == "size_mismatch"


def test_10b_size_mismatch_bigger_aborts_early(tmp_path: Path) -> None:
    c = FakeClock()
    big = DRAFT + bytes(1_000_000)
    a = server(c, "/w/op/", files={"model": MODEL, "draft": big}, throttle_bps=1e6)
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    t0 = c()
    check_done(env, env.run())
    assert c() - t0 < 0.5  # aborted long before the 1 MB tail would have been received
    assert env.events[-1].failovers[0]["reason"] == "size_mismatch"


def test_11_completed_files_make_no_requests(tmp_path: Path) -> None:
    c = FakeClock()
    s = server(c, "/w/op/")
    env = make(tmp_path, {"a": s}, [src("a", A_BASE)], c)
    env.run()
    n = len(s.log)
    env2 = make(tmp_path, {"a": s}, [src("a", A_BASE)], c)
    check_done(env2, env2.run())
    assert len(s.log) == n


def test_12_not_enough_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = FakeClock()
    s = server(c, "/w/op/")
    env = make(tmp_path, {"a": s}, [src("a", A_BASE)], c)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(fetch_mod.shutil, "disk_usage", lambda p: Usage(10, 10, 100))
    with pytest.raises(FetchError) as e:
        env.run()
    assert e.value.reason == "disk" and not s.log


def test_13_credentials_only_to_own_host(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    c = FakeClock()
    a = server(c, "/w/op/", status_for={"/w/op/d.gguf": 503, "/w/op/m.gguf": 503})
    cdn = "https://cdn.other/blob/"
    b = server(
        c,
        "/repo/resolve/rev/",
        redirect_to={
            "/repo/resolve/rev/d.gguf": cdn + "d",
            "/repo/resolve/rev/m.gguf": cdn + "m",
        },
    )
    b.files.update({"/blob/d": DRAFT, "/blob/m": MODEL})
    env = make(
        tmp_path,
        {"a": a, "b": b},
        [src("a", A_BASE, auth="weights_token"), src("b", B_BASE)],
        c,
        secrets={"LLMHUB_WEIGHTS_TOKEN": TOKEN, "HF_TOKEN": HF},
    )
    check_done(env, env.run())
    assert {r.authorization for r in a.log} == {f"Bearer {TOKEN}"}
    hf_reqs = [r for r in b.log if r.host == "huggingface.co"]
    assert hf_reqs and all(r.authorization == f"Bearer {HF}" for r in hf_reqs)
    cdn_reqs = [r for r in b.log if r.host == "cdn.other"]
    assert cdn_reqs and all(r.authorization is None for r in cdn_reqs)
    assert TOKEN not in caplog.text and HF not in caplog.text


def test_13b_hf_token_not_sent_to_self_hosted(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/")
    env = make(tmp_path, {"a": a}, [src("a", A_BASE)], c, secrets={"HF_TOKEN": HF})
    env.run()
    assert all(r.authorization is None for r in a.log)


def test_13c_redirect_to_http_refused(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", redirect_to={"/w/op/d.gguf": "http://evil.test/x"})
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert not any(r.host == "evil.test" for r in a.log)


def test_13d_tls_wrong_certificate_fails_over(tmp_path: Path) -> None:
    pinned = make_self_signed("127.0.0.1")
    other = make_self_signed("127.0.0.1")
    c = FakeClock()
    with TlsServer(other.cert_pem, other.key_pem, MODEL) as ts:
        base = f"https://127.0.0.1:{ts.port}/"
        b = server(c, "/repo/resolve/rev/", files={"model": MODEL})
        plan = parse_plan(
            make_plan_json(
                {"model": MODEL},
                [
                    {
                        "name": "a",
                        "base_url": base,
                        "paths": {"model": "m.gguf"},
                        "ca_pem": pinned.cert_pem,
                    },
                    {"name": "b", "base_url": B_BASE, "paths": {"model": "m.gguf"}},
                ],
                retries_per_source=0,
                read_timeout_s=3,
                connect_timeout_s=3,
            )
        )
        events: list[Progress] = []

        def factory(s):  # real transport for the pinned source, fake for the fallback
            return b if s.name == "b" else fetch_mod._default_transport(s)

        f = Fetcher(
            plan, tmp_path, {}, events.append, c, factory, wall=c, sleep=c.sleep, disk_margin=0
        )
        out = f.run()
        assert out["model"].read_bytes() == MODEL
        assert events[-1].failovers[0] == {"source": "a", "file": "model", "reason": "network"}
        # and with the right certificate the pinned source works
        good = make_self_signed("127.0.0.1")
        with TlsServer(good.cert_pem, good.key_pem, MODEL) as ts2:
            plan2 = parse_plan(
                make_plan_json(
                    {"model": MODEL},
                    [
                        {
                            "name": "a",
                            "base_url": f"https://127.0.0.1:{ts2.port}/",
                            "paths": {"model": "m.gguf"},
                            "ca_pem": good.cert_pem,
                        }
                    ],
                )
            )
            d2 = tmp_path / "ok"
            out2 = Fetcher(
                plan2, d2, {}, lambda p: None, c, wall=c, sleep=c.sleep, disk_margin=0
            ).run()
            assert out2["model"].read_bytes() == MODEL


def test_14_source_sticks_between_files(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", status_for={"/w/op/d.gguf": 403})
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert not a.requests_for("/w/op/m.gguf")


def test_15_progress_monotonic_and_total(tmp_path: Path) -> None:
    c = FakeClock()
    s = server(c, "/w/op/", throttle_bps=1e6, drop_after_bytes=70_000)
    plan = parse_plan(make_plan_json(FILES, [src("a", A_BASE)]))
    ev: list[Progress] = []
    Fetcher(
        plan,
        tmp_path,
        {},
        ev.append,
        c,
        lambda _s: s,
        wall=c,
        sleep=c.sleep,
        disk_margin=0,
        progress_interval_s=0.01,
    ).run()
    dones = [e.done for e in ev]
    assert dones == sorted(dones) and len(ev) > 5
    assert {e.total for e in ev} == {len(MODEL) + len(DRAFT)}
    assert ev[-1].done == ev[-1].total
    assert all(e.sources_left == 0 for e in ev)


def test_15b_deadline_fails(tmp_path: Path) -> None:
    c = FakeClock()
    s = server(c, "/w/op/", throttle_bps=10 * 1024)
    env = make(tmp_path, {"a": s}, [src("a", A_BASE)], c, deadline=c() + 3)
    with pytest.raises(FetchError) as e:
        env.run()
    assert e.value.reason == "deadline"
    assert env.events[-1].phase == "failed" and env.events[-1].error == "deadline"


# ------------------------------------------------------------------ segments
def seg(prefix: str, base: str, n: int = 4, **kw: object) -> dict:
    return src("a", base, segments=n, **kw)


def test_16_segments_assemble_bytewise(tmp_path: Path) -> None:
    c = FakeClock()
    s = server(c, "/w/op/")
    env = make(tmp_path, {"a": s}, [seg("", A_BASE)], c)
    check_done(env, env.run())
    ranges = {r.range for r in s.requests_for("/w/op/m.gguf")}
    assert len(ranges) == 7 and None not in ranges
    assert not (tmp_path / "model.gguf.part.json").exists()


def test_17_one_segment_drops_only_that_chunk_refetched(tmp_path: Path) -> None:
    c = FakeClock()
    s = server(c, "/w/op/", drop_after_bytes=10_000)  # first response only
    env = make(tmp_path, {"a": s}, [seg("", A_BASE)], c)
    check_done(env, env.run())
    reqs = s.requests_for("/w/op/d.gguf")
    # 2 chunks + one resumed tail of the dropped chunk; no chunk fetched twice from its start
    starts = sorted(r.range for r in reqs if r.range)
    assert len(starts) == 3
    assert len({r.range.split("-")[0] for r in reqs if r.range}) == 3


def test_18_failover_mid_segments(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/")
    a.status_for["/w/op/m.gguf"] = 403  # draft fine on a, model fails -> hf
    b = server(c, "/repo/resolve/rev/")
    # first source gives 2 chunks of the model before failing
    orig = a.handle_request
    cnt = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("m.gguf"):
            cnt["n"] += 1
            if cnt["n"] <= 2:
                a.status_for.pop("/w/op/m.gguf", None)
            else:
                a.status_for["/w/op/m.gguf"] = 403
        return orig(req)

    a.handle_request = h  # type: ignore[method-assign]
    env = make(
        tmp_path,
        {"a": a, "b": b},
        [seg("", A_BASE, n=2), src("b", B_BASE, segments=2)],
        c,
        chunk=32 * 1024,
    )
    check_done(env, env.run())
    assert env.events[-1].failovers[0]["file"] == "model"


def test_19_restart_with_sidecar_fetches_only_missing(tmp_path: Path) -> None:
    c = FakeClock()
    chunk = 32 * 1024
    # simulate a crashed run: chunks 0 and 2 of the model are on disk and recorded
    part = bytearray(len(MODEL))
    for i in (0, 2):
        part[i * chunk : (i + 1) * chunk] = MODEL[i * chunk : (i + 1) * chunk]
    (tmp_path / "model.gguf.part").write_bytes(bytes(part))
    import json

    (tmp_path / "model.gguf.part.json").write_text(
        json.dumps({"v": 1, "size": len(MODEL), "chunk": chunk, "done": [0, 2]})
    )
    s = server(c, "/w/op/")
    env = make(tmp_path, {"a": s}, [seg("", A_BASE)], c, chunk=chunk)
    check_done(env, env.run())
    got = {r.range for r in s.requests_for("/w/op/m.gguf")}
    assert f"bytes=0-{chunk - 1}" not in got and f"bytes={2 * chunk}-{3 * chunk - 1}" not in got
    assert len(got) == 7 - 2


def test_19b_segmented_sha_mismatch_next_source(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", corrupt={"/w/op/d.gguf"})
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [seg("", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())
    assert env.events[-1].failovers[0]["reason"] == "sha_mismatch"


def test_19c_segment_cdn_target_cached_and_refreshed_on_403(tmp_path: Path) -> None:
    c = FakeClock()
    cdn = "https://cdn.other/blob/"
    s = server(c, "/repo/resolve/rev/", redirect_to={"/repo/resolve/rev/d.gguf": cdn + "d"})
    s.files["/blob/d"] = DRAFT
    s.files["/repo/resolve/rev/m.gguf"] = MODEL
    plan = parse_plan(make_plan_json(FILES, [src("a", B_BASE, segments=2)]))
    Fetcher(
        plan,
        tmp_path,
        {},
        lambda p: None,
        c,
        lambda _s: s,
        wall=c,
        sleep=c.sleep,
        chunk_size=32 * 1024,
        disk_margin=0,
    ).run()
    resolves = [r for r in s.log if r.host == "huggingface.co" and r.path.endswith("d.gguf")]
    assert len(resolves) < 2 + 1  # cached after the first resolution (at most one racing extra)

    # now the CDN link expires: 403 from the CDN -> one fresh resolve
    c2 = FakeClock()
    s2 = server(c2, "/repo/resolve/rev/", redirect_to={"/repo/resolve/rev/d.gguf": cdn + "d"})
    s2.files["/blob/d"] = DRAFT
    s2.files["/repo/resolve/rev/m.gguf"] = MODEL
    s2.fail_first_n = {}
    orig = s2.handle_request
    seen = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.host == "cdn.other":
            seen["n"] += 1
            if seen["n"] == 2:
                return httpx.Response(403)
        return orig(req)

    s2.handle_request = h  # type: ignore[method-assign]
    d2 = tmp_path / "x"
    out = Fetcher(
        parse_plan(make_plan_json(FILES, [src("a", B_BASE, segments=2)])),
        d2,
        {},
        lambda p: None,
        c2,
        lambda _s: s2,
        wall=c2,
        sleep=c2.sleep,
        chunk_size=32 * 1024,
        disk_margin=0,
    ).run()
    assert out["draft"].read_bytes() == DRAFT


# ------------------------------------------------------------------ progress file
def test_20_progress_file_atomic_0600_and_corrupt(tmp_path: Path) -> None:
    p = Progress("downloading", "model", "a", 1, 2, 0.5, [], None)
    path = tmp_path / "progress.json"
    write_progress(path, p)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert read_progress(path)["done"] == 1  # type: ignore[index]
    assert not list(tmp_path.glob("*.tmp"))
    path.write_text("{broken")
    assert read_progress(path) is None
    assert read_progress(tmp_path / "missing.json") is None
