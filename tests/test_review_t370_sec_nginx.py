"""T-370 security review, cycle 1: real nginx + validator probes (docker, reuses the D5 rig).

Tests that document an OPEN finding are `xfail(strict=True)` with the finding id in the
reason: they flip to XPASS (and fail the run) once the fix lands, so the marker must be
removed together with the fix.

Fix round 1: all findings covered here are fixed; the xfail markers were removed.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

import pytest

from test_w_nginx_conf import MODEL, OP, Rig, _wait_log, docker, needs_docker, rig

__all__ = ["rig"]  # the fixture is imported for pytest


def _served(r: Rig) -> int:
    acct = r.w / "log" / "weights-acct.log"
    total = 0
    for line in acct.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == OP:
            total += int(parts[1])
    return total


def _all_logs(r: Rig) -> str:
    texts = [p.read_text() for p in (r.w / "log").iterdir()]
    texts.append(
        subprocess.run(
            ["docker", "logs", r.name], check=False, capture_output=True, text=True
        ).stderr
    )
    return "\n".join(texts)


@docker
@needs_docker
def test_forged_proxy_headers_are_overridden(rig: Callable[..., Rig]) -> None:
    """Client-sent X-Real-IP / X-Original-URI / X-Range must not reach the validator."""
    r = rig()
    with r.client() as c:
        h = {**r.headers(), "X-Real-IP": "203.0.113.9"}
        assert c.get(f"/w/{OP}/qwen/draft.gguf", headers=h).status_code == 200
        # bound IP is the real peer (127.0.0.1), not the forged one
        h2 = {**r.headers(), "X-Real-IP": "127.0.0.1"}
        assert c.get(f"/w/{OP}/qwen/draft.gguf", headers=h2).status_code == 200
        # a forged X-Original-URI cannot authorize a path the location did not match
        h3 = {**r.headers(), "X-Original-URI": f"/w/{OP}/qwen/model.gguf"}
        assert c.get(f"/w/{OP}/qwen/model.txt", headers=h3).status_code == 404
        _wait_log(r.w / "log" / "weights-acct.log", f"{OP} ")
        c.get(f"/w/{OP}/qwen/model.gguf", headers=r.headers())  # served ~ full set
        time.sleep(0.3)
        # cap reached: a forged small X-Range must not make the validator think "small"
        h4 = {**r.headers(), "X-Range": "bytes=0-0"}
        assert c.get(f"/w/{OP}/qwen/model.gguf", headers=h4).status_code == 403


@docker
@needs_docker
def test_query_string_and_head(rig: Callable[..., Rig]) -> None:
    r = rig()
    with r.client() as c:
        # any query is refused by the validator (raw target contains '?')
        q = c.get(f"/w/{OP}/qwen/draft.gguf?token=x", headers=r.headers())
        assert q.status_code == 403
        head = c.head(f"/w/{OP}/qwen/draft.gguf")
        assert head.status_code == 401
    assert "?" not in (r.w / "log" / "weights-access.log").read_text()


@docker
@needs_docker
def test_error_log_has_no_token_on_limit_conn(rig: Callable[..., Rig]) -> None:
    """limit_conn rejections are logged at warn in weights-error.log: no token there."""
    r = rig(rate="1k", segments=1)
    with r.client() as c1, r.client() as c2:  # noqa: SIM117
        with c1.stream("GET", f"/w/{OP}/qwen/big.gguf", headers=r.headers()) as resp:
            it = resp.iter_raw(1024)
            next(it)
            assert c2.get(f"/w/{OP}/qwen/draft.gguf", headers=r.headers()).status_code == 503
    time.sleep(0.3)
    err = (r.w / "log" / "weights-error.log").read_text()
    assert "limiting connections" in err
    blob = _all_logs(r)
    assert r.token not in blob and r.token.split(".")[3] not in blob and "Bearer" not in blob


@docker
@needs_docker
def test_multi_range_cannot_amplify(rig: Callable[..., Rig]) -> None:
    """Overlapping multi-range: nginx must not send more than the file (Apache-killer)."""
    r = rig()
    rng = "bytes=" + ",".join(["0-"] * 50)
    with r.client() as c:
        resp = c.get(f"/w/{OP}/qwen/model.gguf", headers={**r.headers(), "Range": rng})
    assert resp.status_code in (200, 206, 416)
    assert len(resp.content) <= len(MODEL) + 4096


@docker
@needs_docker
def test_if_range_does_not_bypass_volume_cap(rig: Callable[..., Rig]) -> None:
    r = rig()
    with r.client() as c:
        assert c.get(f"/w/{OP}/qwen/model.gguf", headers=r.headers()).status_code == 200
        _wait_log(r.w / "log" / "weights-acct.log", f"{OP} ")
        # served 3072 of cap 3840: a 10-byte Range passes the cap check...
        h = {**r.headers(), "Range": "bytes=0-9", "If-Range": '"bogus-etag"'}
        resp = c.get(f"/w/{OP}/qwen/model.gguf", headers=h)
    # ...and must not be turned into a full 200 body
    assert not (resp.status_code == 200 and len(resp.content) == len(MODEL))


@docker
@needs_docker
def test_unauthenticated_requests_do_not_consume_victim_volume(rig: Callable[..., Rig]) -> None:
    r = rig()
    with r.client() as c:
        for _ in range(5):
            assert c.get(f"/w/{OP}/qwen/model.gguf").status_code == 401
    time.sleep(0.3)
    assert _served(r) == 0


@docker
@needs_docker
@pytest.mark.parametrize("rng", ["bytes=5-3", "bytes=-0", "bytes=0-9", "bytes=-10", "bytes=3070-"])
def test_cheap_ranges_never_send_more_than_charged(rig: Callable[..., Rig], rng: str) -> None:
    """Whatever the validator charges for a Range, nginx must not send more (no If-Range)."""
    from llmhub.weights.auth_app import requested_bytes

    r = rig()
    with r.client() as c:
        resp = c.get(f"/w/{OP}/qwen/model.gguf", headers={**r.headers(), "Range": rng})
    assert len(resp.content) <= max(requested_bytes(rng, len(MODEL)), 0) or resp.status_code == 416
