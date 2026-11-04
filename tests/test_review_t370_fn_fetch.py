"""Review T-370 cycle 2: downloader edge cases (failover robustness, content coding, resume).

Passing tests pin behaviour confirmed in review; xfail(strict=True) ones document defects.

Fix round 1: all findings covered here are fixed; the xfail markers were removed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from llmhub_infer.fetch import FetchError

from i_fakes import FakeClock
from test_i_fetch import A_BASE, B_BASE, FILES, check_done, make, server, src


def test_bad_redirect_location_fails_over(tmp_path: Path) -> None:
    """A malformed Location on source a is a source failure (failover), not a pod crash."""
    c = FakeClock()
    a = server(c, "/w/op/", redirect_to={"/w/op/d.gguf": "https://[::1/bad"})
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"a": a, "b": b}, [src("a", A_BASE), src("b", B_BASE)], c)
    check_done(env, env.run())


def test_requests_identity_encoding(tmp_path: Path) -> None:
    import httpx

    seen: list[str | None] = []
    c = FakeClock()
    a = server(c, "/w/op/")
    inner = a.handle_request

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return inner(request)

    a.handle_request = spy  # type: ignore[method-assign]
    env = make(tmp_path, {"a": a}, [src("a", A_BASE)], c)
    env.run()
    assert seen and all(v == "identity" for v in seen), seen


def test_segmented_then_single_source_resumes_prefix_only(tmp_path: Path) -> None:
    """Segments done out of order on source a; b (1 segment) keeps only the contiguous prefix."""
    c = FakeClock()
    chunk = 32 * 1024
    model = FILES["model"]
    part = tmp_path / "model.gguf.part"
    part.write_bytes(model[: 2 * chunk] + b"\0" * chunk + model[3 * chunk : 4 * chunk])
    part.write_bytes(part.read_bytes() + b"\0" * (len(model) - part.stat().st_size))
    side = tmp_path / "model.gguf.part.json"
    side.write_text(json.dumps({"v": 1, "size": len(model), "chunk": chunk, "done": [0, 1, 3]}))
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"b": b}, [src("b", B_BASE, segments=1)], c, chunk=chunk)
    check_done(env, env.run())
    reqs = b.requests_for("/repo/resolve/rev/m.gguf")
    assert reqs[0].range == f"bytes={2 * chunk}-"
    assert not side.exists()


def test_stale_sidecar_other_chunk_size_is_ignored(tmp_path: Path) -> None:
    c = FakeClock()
    model = FILES["model"]
    (tmp_path / "model.gguf.part").write_bytes(b"\xff" * len(model))  # garbage, full size
    (tmp_path / "model.gguf.part.json").write_text(
        json.dumps({"v": 1, "size": len(model), "chunk": 999, "done": [0, 1, 2]})
    )
    b = server(c, "/repo/resolve/rev/")
    env = make(tmp_path, {"b": b}, [src("b", B_BASE, segments=4)], c, chunk=32 * 1024)
    out = env.run()
    assert hashlib.sha256(out["model"].read_bytes()).hexdigest() == hashlib.sha256(
        model
    ).hexdigest()


def test_deadline_during_retry_backoff_is_deadline_not_exhausted(tmp_path: Path) -> None:
    c = FakeClock()
    a = server(c, "/w/op/", fail_first_n={"/w/op/d.gguf": 100})
    env = make(tmp_path, {"a": a}, [src("a", A_BASE)], c, deadline=c() + 1.5)
    with pytest.raises(FetchError) as ei:
        env.run()
    assert ei.value.reason == "deadline"
    assert env.events[-1].phase == "failed" and env.events[-1].error == "deadline"
