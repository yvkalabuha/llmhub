"""T-370 review round 2: real nginx probes of the round-1 fixes (docker, reuses the D5 rig)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from test_w_nginx_conf import MODEL, OP, Rig, _wait_log, docker, needs_docker, rig

__all__ = ["rig"]  # the fixture is imported for pytest


@docker
@needs_docker
@pytest.mark.parametrize("if_range", ["", '"bogus"', "Thu, 01 Jan 1970 00:00:00 GMT"])
def test_any_if_range_value_cannot_bypass_volume_cap(
    rig: Callable[..., Rig], if_range: str
) -> None:
    """R2-S1: an EMPTY If-Range also makes nginx ignore Range (full 200); it must be charged."""
    r = rig()
    with r.client() as c:
        assert c.get(f"/w/{OP}/qwen/model.gguf", headers=r.headers()).status_code == 200
        _wait_log(r.w / "log" / "weights-acct.log", f"{OP} ")
        h = {**r.headers(), "Range": "bytes=0-9", "If-Range": if_range}
        resp = c.get(f"/w/{OP}/qwen/model.gguf", headers=h)
    assert not (resp.status_code == 200 and len(resp.content) == len(MODEL)), resp.status_code
