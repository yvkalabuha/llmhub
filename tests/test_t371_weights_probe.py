"""T-371 F2: weights probe - pinned Range GET, negatives, MemAvailable abort, no token output."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from llmhub.configuration.models import Config
from llmhub.tools import weights_probe as wp
from llmhub.weights.tokens import Verdict, check_request

ROOT = Path(__file__).resolve().parent.parent
SECRET = b"p" * 40
OP = "0123456789ab"
REL = "qwen/model.gguf"
DATA = bytes(range(256)) * 8192  # 2 MiB


def _gen() -> Any:
    spec = importlib.util.spec_from_file_location("gen_cert_probe", ROOT / "deploy/gen_weights_cert.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_cert_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def _cfg(tmp: Path, min_mem: int = 8192) -> Config:
    _gen().generate("127.0.0.1", tmp)
    return Config.model_validate({
        "data_dir": tmp / "data", "pools": {},
        "weights": {"min_mem_available_mib": min_mem, "sources": [{
            "name": "self", "kind": "self_hosted", "base_url": "https://127.0.0.1:9443/w/{op_id}",
            "paths": {"model": REL}, "ca_pem_file": str(tmp / "weights-cert.pem")}]},
    })  # fmt: skip


def _meminfo(tmp: Path, gib: int) -> Path:
    p = tmp / f"meminfo-{gib}"
    p.write_text(f"MemTotal: 65000000 kB\nMemAvailable: {gib * 1024 * 1024} kB\n")
    return p


class FakeNginx:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        v, _ = check_request(SECRET, req.headers.get("authorization"), req.url.raw_path.decode(),
                             time.time(), lambda op: "BOOTING" if op == OP else None,
                             frozenset({REL}))  # fmt: skip
        if v is Verdict.MISSING:
            return httpx.Response(401)
        if v is not Verdict.OK:
            return httpx.Response(403)
        rng = req.headers["range"].removeprefix("bytes=")
        a, b = (int(x) for x in rng.split("-"))
        return httpx.Response(206, content=DATA[a : b + 1])


def test_probe_ok_with_expected_sha(tmp_path: Path) -> None:
    fake = FakeNginx()
    want = hashlib.sha256(DATA[: wp.PROBE_BYTES]).hexdigest()
    code, rep = wp.run_probe(_cfg(tmp_path), SECRET, OP, expect_sha256=want, negatives=True,
                             transport=httpx.MockTransport(fake),
                             meminfo=_meminfo(tmp_path, 31))  # fmt: skip
    assert code == 0, rep
    names = {c["name"]: c for c in rep["checks"]}
    assert names["range_206"]["received"] == wp.PROBE_BYTES
    assert {"other_op_403", "expired_token_403", "no_token_401"} <= set(names)
    assert fake.seen[0].url.path == f"/w/{OP}/{REL}"
    assert fake.seen[0].headers["range"] == f"bytes=0-{wp.PROBE_BYTES - 1}"
    text = json.dumps(rep)
    for r in fake.seen:
        auth = r.headers.get("authorization")
        if auth:
            assert auth.split()[1] not in text


def test_sha_mismatch_fails(tmp_path: Path) -> None:
    code, rep = wp.run_probe(_cfg(tmp_path), SECRET, OP, expect_sha256="0" * 64,
                             transport=httpx.MockTransport(FakeNginx()),
                             meminfo=_meminfo(tmp_path, 31))  # fmt: skip
    assert code == 1 and rep["ok"] is False


def test_low_memory_aborts_before_request(tmp_path: Path) -> None:
    fake = FakeNginx()
    code, _ = wp.run_probe(_cfg(tmp_path), SECRET, OP, transport=httpx.MockTransport(fake),
                           meminfo=_meminfo(tmp_path, 4))  # fmt: skip
    assert code == wp.EXIT_LOW_MEMORY and fake.seen == []


def test_threshold_zero_skips_memory_check(tmp_path: Path) -> None:
    code, _ = wp.run_probe(_cfg(tmp_path, min_mem=0), SECRET, OP,
                           transport=httpx.MockTransport(FakeNginx()),
                           meminfo=_meminfo(tmp_path, 1))  # fmt: skip
    assert code == 0


def test_no_self_hosted_source_is_config_error(tmp_path: Path) -> None:
    cfg = Config.model_validate({"data_dir": tmp_path, "pools": {}})
    with pytest.raises(wp.ConfigError):
        wp.target(cfg, OP, None)


def test_cli_missing_secret_is_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(f"data_dir: {tmp_path}\npools: {{}}\n")
    assert wp.main(["--config", str(p), "--op", OP], env={}) == 2


def test_latest_loading_op(tmp_path: Path) -> None:
    from llmhub.persistence.db import connect, migrate

    db = tmp_path / "state.db"
    conn = connect(db)
    migrate(conn)
    for i, (op, state, t) in enumerate([("aaaaaaaaaaaa", "BOOTING", 1), ("bbbbbbbbbbbb", "IDLE", 3),
                                        (OP, "LOADING_MODEL", 2)]):  # fmt: skip
        conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
            " created_at, max_lifetime_at) VALUES (?, ?, ?, 'mock', ?, ?, 0, ?, 1e12)",
            (f"i{i}", op, f"p{i}", f"l{i}", state, t),
        )
    conn.close()
    assert wp.latest_loading_op(db) == OP
