"""E3: controller env contract -> pod parser. The only test importing both packages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from llmhub_infer.supervisor import child_env, read_env

from llmhub.cloud_manager.instance_env import build_material
from w_fixtures import CA_PEMS, SECRETS, inst_row, weights_cfg

OP = "0123456789ab"


@pytest.mark.parametrize("transport", ["proxy", "direct"])
def test_material_parses_in_pod(tmp_path: Path, transport: str) -> None:
    cfg = weights_cfg(tmp_path, transport=transport)
    pool = cfg.pools["p1"]
    m = build_material(inst_row(OP), pool, cfg, SECRETS, CA_PEMS)
    pe = read_env(m.env | m.secret_env)
    assert pe.op_id == OP and pe.transport == transport
    assert pe.port == pool.infer.effective_port
    assert pe.llama_args == pool.infer.llama_args
    assert pe.api_key == m.secret_env["LLMHUB_API_KEY"]
    assert pe.weights_token == m.secret_env["LLMHUB_WEIGHTS_TOKEN"]
    assert str(pe.weights_dir) == pool.infer.weights_dir
    if transport == "direct":
        assert pe.tls_cert_pem == m.secret_env["LLMHUB_TLS_CERT_PEM"]
        assert pe.tls_key_pem == m.secret_env["LLMHUB_TLS_KEY_PEM"]
    else:
        assert pe.tls_cert_pem is None and pe.tls_key_pem is None
    assert pe.hf_token is None
    # the plan, field by field
    raw = json.loads(m.env["LLMHUB_WEIGHTS_PLAN"])
    plan = pe.plan
    assert [(f.id, f.size, f.sha256) for f in plan.files] == [
        (f["id"], f["size"], f["sha256"]) for f in raw["files"]
    ]
    assert plan.deadline_unix == raw["deadline_unix"]
    for key in ("speed_grace_s", "speed_window_s", "read_timeout_s", "connect_timeout_s",
                "retries_per_source"):  # fmt: skip
        assert getattr(plan, key) == raw[key] == getattr(cfg.weights, key)
    assert len(plan.sources) == len(raw["sources"]) == 2
    for s, r in zip(plan.sources, raw["sources"], strict=True):
        assert (s.name, s.base_url, dict(s.paths), s.ca_pem, s.auth, s.min_speed_mb_s,
                s.segments) == (r["name"], r["base_url"], r["paths"], r["ca_pem"], r["auth"],
                                r["min_speed_mb_s"], r["segments"])  # fmt: skip
    # nothing secret reaches llama-server
    child = child_env(m.env | m.secret_env | {"PATH": "/usr/bin"})
    assert child == {"PATH": "/usr/bin"}


def test_hf_token_contract(tmp_path: Path) -> None:
    from llmhub.cloud_manager.instance_env import InstanceSecrets

    cfg = weights_cfg(tmp_path, hf_token_env="HF_READ_TOKEN")
    secrets = InstanceSecrets(SECRETS.instance, SECRETS.weights, hf_token="hf_xxx")
    m = build_material(inst_row(OP), cfg.pools["p1"], cfg, secrets, CA_PEMS)
    assert read_env(m.env | m.secret_env).hf_token == "hf_xxx"
