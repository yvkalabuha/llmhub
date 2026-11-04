"""F (deferred from D5): controller material -> pod Fetcher -> real nginx + validator (docker).

The plan and the token are built by the controller (`build_material`), parsed by the pod
(`read_env`) and the pod `Fetcher` downloads through the real nginx container with
`auth_request` to the validator; "hf" is an in-process fake.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from llmhub_infer.fetch import Fetcher, Progress, _default_transport
from llmhub_infer.plan import SourceSpec
from llmhub_infer.supervisor import read_env

from i_fakes import FakeClock, FakeWeightsServer
from llmhub.cloud_manager.instance_env import InstanceSecrets, build_material
from llmhub.configuration.models import Config
from test_w_nginx_conf import DRAFT, MODEL, OP, SECRET, Rig, docker, needs_docker, rig
from w_fixtures import INSTANCE_SECRET

__all__ = ["rig"]  # the fixture is shared with the D5 tests

HF_BASE = "https://huggingface.co/org/repo/resolve/abc/"


def _cfg(r: Rig, tmp: Path) -> Config:
    ca = tmp / "weights-ca.pem"
    ca.write_text((r.w / "cert" / "weights-cert.pem").read_text())
    pool = {
        "name": "p1", "provider": "mock", "gpu": "RTX4090", "vram_gb": 24, "pp_tps": 1200,
        "tg_tps": 78, "image": "llmhub-infer:test", "disk_gb": 50, "models": ["m1"],
        "weights": "set1",
    }  # fmt: skip
    files = [
        {"id": "model", "size": len(MODEL), "sha256": hashlib.sha256(MODEL).hexdigest()},
        {"id": "draft", "size": len(DRAFT), "sha256": hashlib.sha256(DRAFT).hexdigest()},
    ]
    sources = [
        {"name": "self_hosted", "kind": "self_hosted",
         "base_url": f"https://127.0.0.1:{r.port}/w/{{op_id}}/", "ca_pem_file": str(ca),
         "segments": 1, "paths": {"model": "qwen/model.gguf", "draft": "qwen/draft.gguf"}},
        {"name": "hf", "kind": "hf", "base_url": HF_BASE, "min_speed_mb_s": 0, "segments": 1,
         "paths": {"model": "model.gguf", "draft": "MTP/draft.gguf"}},
    ]  # fmt: skip
    return Config.model_validate(
        {
            "data_dir": tmp,
            "pools": {"p1": pool},
            "weights": {
                "sources": sources,
                "sets": {"set1": {"files": files}},
                "speed_grace_s": 30,
                "retries_per_source": 0,
            },
        }
    )


def _fetch(r: Rig, tmp: Path) -> tuple[dict[str, Path], FakeWeightsServer, Fetcher, str]:
    cfg = _cfg(r, tmp)
    secrets = InstanceSecrets(instance=INSTANCE_SECRET, weights=SECRET)  # validator's secret
    row = {"op_id": OP, "created_at": time.time(), "id": "inst-a"}
    m = build_material(row, cfg.pools["p1"], cfg, secrets, {"self_hosted": cfg_ca(tmp)})
    pe = read_env(m.env | m.secret_env)
    hf = FakeWeightsServer(
        files={"/org/repo/resolve/abc/model.gguf": MODEL, "/org/repo/resolve/abc/MTP/draft.gguf":
               DRAFT}, clock=FakeClock(),
    )  # fmt: skip

    def transport(src: SourceSpec) -> httpx.BaseTransport:
        return hf if src.name == "hf" else _default_transport(src)  # real TLS + pin to nginx

    secrets_env = {"LLMHUB_WEIGHTS_TOKEN": pe.weights_token or ""}  # as the supervisor does
    progress: list[Progress] = []
    dest = tmp / "pod-models"
    dest.mkdir(exist_ok=True)
    f = Fetcher(pe.plan, dest, secrets_env, progress.append, transport_factory=transport)
    return f.run(), hf, f, m.secret_env["LLMHUB_WEIGHTS_TOKEN"]


def cfg_ca(tmp: Path) -> str:
    return (tmp / "weights-ca.pem").read_text()


def _acct(r: Rig) -> str:
    return (r.w / "log" / "weights-acct.log").read_text()


@docker
@needs_docker
def test_fetcher_downloads_through_nginx_with_token(
    rig: Callable[..., Rig], tmp_path: Path
) -> None:
    r = rig()
    out, hf, _, token = _fetch(r, tmp_path)
    assert out["model"].read_bytes() == MODEL and out["draft"].read_bytes() == DRAFT
    assert hf.log == []  # everything came from our server
    deadline = time.time() + 5
    while OP not in _acct(r) and time.time() < deadline:
        time.sleep(0.05)
    assert OP in _acct(r)
    sig = token.rsplit(".", 1)[1]
    for log in ("weights-acct.log", "weights-access.log", "weights-error.log"):
        p = r.w / "log" / log
        assert not p.exists() or sig not in p.read_text()


@docker
@needs_docker
def test_fetcher_resumes_with_range_through_nginx(rig: Callable[..., Rig], tmp_path: Path) -> None:
    r = rig()
    dest = tmp_path / "pod-models"
    dest.mkdir()
    (dest / "model.gguf.part").write_bytes(MODEL[:1000])  # interrupted earlier
    out, hf, _, _ = _fetch(r, tmp_path)
    assert out["model"].read_bytes() == MODEL and hf.log == []
    access = r.w / "log" / "weights-access.log"
    deadline = time.time() + 5
    while " 206 " not in access.read_text() and time.time() < deadline:
        time.sleep(0.05)
    assert " 206 " in access.read_text()


@docker
@needs_docker
def test_after_ready_token_revoked_fetcher_fails_over_to_hf(
    rig: Callable[..., Rig], tmp_path: Path
) -> None:
    r = rig()
    r.conn.execute("UPDATE instances SET state='READY'")
    out, hf, f, _ = _fetch(r, tmp_path)
    assert out["model"].read_bytes() == MODEL and out["draft"].read_bytes() == DRAFT
    assert {q.path for q in hf.log} == {
        "/org/repo/resolve/abc/model.gguf", "/org/repo/resolve/abc/MTP/draft.gguf"
    }  # fmt: skip
    assert all(q.authorization is None for q in hf.log)  # the weights token never goes to HF
    assert f._failovers and f._failovers[0]["source"] == "self_hosted"
