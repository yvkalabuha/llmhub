"""E1/E2: InstanceSpec.secret_env, derive_api_key / derive_tls, build_material (T-370)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ed25519

from llmhub.cloud_manager.instance_env import (
    InstanceSecrets,
    build_material,
    derive_api_key,
    derive_tls,
    load_instance_secrets,
    make_material_fns,
)
from llmhub.configuration.loader import ConfigError
from llmhub.provider_adapters.base import InstanceSpec
from llmhub.weights.tokens import Verdict, check_request, issue_token, token_expiry
from w_fixtures import (
    CA_PEMS,
    HF_SECRET,
    INSTANCE_SECRET,
    SECRETS,
    WEIGHTS_SECRET,
    inst_row,
    weights_cfg,
)

OP = "0123456789ab"
T0 = 1_762_000_000.0


def test_spec_repr_hides_secret_env() -> None:
    spec = InstanceSpec(
        pool="p", offer_id="o", image="i", disk_gb=1, max_price_usd_h=1.0, max_lifetime_s=600,
        env={"A": "b"}, secret_env={"LLMHUB_API_KEY": "very-secret-value"},
    )  # fmt: skip
    assert "very-secret-value" not in repr(spec) and "secret_env" not in repr(spec)
    old = InstanceSpec(pool="p", offer_id="o", image="i", disk_gb=1, max_price_usd_h=1.0,
                       max_lifetime_s=600, env={})  # fmt: skip
    assert dict(old.secret_env) == {}


def test_secrets_repr_hidden() -> None:
    s = InstanceSecrets(instance=INSTANCE_SECRET, weights=WEIGHTS_SECRET, hf_token=HF_SECRET)
    r = repr(s)
    assert INSTANCE_SECRET.decode() not in r and WEIGHTS_SECRET.decode() not in r
    assert HF_SECRET not in r


def test_derive_api_key_deterministic_per_op() -> None:
    a = derive_api_key(INSTANCE_SECRET, OP)
    assert a == derive_api_key(INSTANCE_SECRET, OP) and len(a) == 43
    assert a != derive_api_key(INSTANCE_SECRET, "ba9876543210")
    assert a != derive_api_key(b"x" * 40, OP)


def test_derive_tls_deterministic_ed25519() -> None:
    c1, k1, f1 = derive_tls(INSTANCE_SECRET, OP, T0)
    c2, k2, f2 = derive_tls(INSTANCE_SECRET, OP, T0)
    assert (c1, k1, f1) == (c2, k2, f2)
    cert = x509.load_pem_x509_certificate(c1.encode())
    assert isinstance(cert.public_key(), ed25519.Ed25519PublicKey)
    from cryptography.hazmat.primitives.serialization import Encoding

    assert hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest() == f1
    life = cert.not_valid_after_utc.timestamp() - T0
    assert life >= 10800  # >= max_lifetime_s cap
    assert cert.not_valid_before_utc.timestamp() <= T0
    assert derive_tls(INSTANCE_SECRET, "ba9876543210", T0)[2] != f1


async def _tls_roundtrip(cert_pem: str, key_pem: str, ca_pem: str, tmp: Path) -> bool:
    (tmp / "c.pem").write_text(cert_pem)
    (tmp / "k.pem").write_text(key_pem)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(tmp / "c.pem", tmp / "k.pem")

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.write(b"hi")
        await w.drain()
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=sctx)
    port = server.sockets[0].getsockname()[1]
    cctx = ssl.create_default_context(cadata=ca_pem)
    cctx.check_hostname = False
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=cctx)
        data = await r.read()
        w.close()
        return data == b"hi"
    except ssl.SSLError:
        return False
    finally:
        server.close()
        await server.wait_closed()


async def test_derive_tls_pin_accepts_own_rejects_other(tmp_path: Path) -> None:
    now = time.time()  # the certificate is valid from created_at - 1 h for 7 days
    cert, key, _ = derive_tls(INSTANCE_SECRET, OP, now)
    other, _, _ = derive_tls(INSTANCE_SECRET, "ba9876543210", now)
    assert await _tls_roundtrip(cert, key, cert, tmp_path)
    assert not await _tls_roundtrip(cert, key, other, tmp_path)


def _all_secret_values(secret_env: dict[str, str]) -> list[str]:
    return list(secret_env.values())


def test_build_material_proxy(tmp_path: Path) -> None:
    cfg = weights_cfg(tmp_path)
    m = build_material(inst_row(OP, T0), cfg.pools["p1"], cfg, SECRETS, CA_PEMS)
    assert m.tls_fingerprint is None
    assert set(m.env) == {
        "LLMHUB_OP_ID", "LLMHUB_TRANSPORT", "LLMHUB_PORT", "LLMHUB_WEIGHTS_PLAN",
        "LLMHUB_LLAMA_ARGS", "LLMHUB_MAX_LIFETIME_S", "LLMHUB_WEIGHTS_DIR",
    }  # fmt: skip
    assert m.env["LLMHUB_TRANSPORT"] == "proxy" and m.env["LLMHUB_PORT"] == "8080"
    assert set(m.secret_env) == {"LLMHUB_API_KEY", "LLMHUB_WEIGHTS_TOKEN"}
    assert m.secret_env["LLMHUB_API_KEY"] == derive_api_key(INSTANCE_SECRET, OP)
    plan = json.loads(m.env["LLMHUB_WEIGHTS_PLAN"])
    assert [f["id"] for f in plan["files"]] == ["draft", "model"]
    sh, hf = plan["sources"]
    assert sh["base_url"] == f"https://203.0.113.7:9443/w/{OP}/" and sh["auth"] == "weights_token"
    assert sh["ca_pem"] == CA_PEMS["self_hosted"] and sh["segments"] == 1
    assert hf["auth"] == "none" and hf["ca_pem"] is None and hf["segments"] == 4
    exp = token_expiry(T0, cfg.timeouts)
    assert plan["deadline_unix"] == exp
    assert m.secret_env["LLMHUB_WEIGHTS_TOKEN"] == issue_token(WEIGHTS_SECRET, OP, exp)
    # the token is valid for the weights validator while loading
    verdict, op = check_request(
        WEIGHTS_SECRET, "Bearer " + m.secret_env["LLMHUB_WEIGHTS_TOKEN"], f"/w/{OP}/q/model.gguf",
        T0 + 60, lambda _o: "LOADING_MODEL", frozenset({"q/model.gguf"}),
    )  # fmt: skip
    assert verdict == Verdict.OK and op == OP
    joined = json.dumps(m.env)
    for v in _all_secret_values(m.secret_env):
        assert v not in joined
    assert "HF_TOKEN" not in m.secret_env  # E2: not passed by default
    assert m.secret_env["LLMHUB_API_KEY"] not in repr(m)


def test_build_material_direct_has_tls(tmp_path: Path) -> None:
    cfg = weights_cfg(tmp_path, transport="direct")
    m = build_material(inst_row(OP, T0), cfg.pools["p1"], cfg, SECRETS, CA_PEMS)
    cert, key, fp = derive_tls(INSTANCE_SECRET, OP, T0)
    assert m.tls_fingerprint == fp and m.env["LLMHUB_PORT"] == "8443"
    assert m.secret_env["LLMHUB_TLS_CERT_PEM"] == cert
    assert m.secret_env["LLMHUB_TLS_KEY_PEM"] == key
    assert key not in json.dumps(m.env) and "PRIVATE KEY" not in json.dumps(m.env)


def test_build_material_without_self_hosted_no_token(tmp_path: Path) -> None:
    cfg = weights_cfg(tmp_path, kinds=("hf",))
    m = build_material(inst_row(OP, T0), cfg.pools["p1"], cfg, SECRETS, {})
    assert "LLMHUB_WEIGHTS_TOKEN" not in m.secret_env


def test_build_material_hf_token_only_if_configured(tmp_path: Path) -> None:
    with_token = InstanceSecrets(INSTANCE_SECRET, WEIGHTS_SECRET, hf_token=HF_SECRET)
    cfg = weights_cfg(tmp_path)
    m = build_material(inst_row(OP, T0), cfg.pools["p1"], cfg, with_token, CA_PEMS)
    assert "HF_TOKEN" not in m.secret_env  # hf_token_env unset -> never passed
    cfg2 = weights_cfg(tmp_path, hf_token_env="HF_READ_TOKEN")
    m2 = build_material(inst_row(OP, T0), cfg2.pools["p1"], cfg2, with_token, CA_PEMS)
    assert m2.secret_env["HF_TOKEN"] == HF_SECRET and HF_SECRET not in json.dumps(m2.env)


def test_build_material_missing_ca_is_config_error(tmp_path: Path) -> None:
    cfg = weights_cfg(tmp_path)
    with pytest.raises(ConfigError):
        build_material(inst_row(OP, T0), cfg.pools["p1"], cfg, SECRETS, {})


def test_load_instance_secrets(tmp_path: Path) -> None:
    env = {
        "LLMHUB_INSTANCE_SECRET": INSTANCE_SECRET.decode(),
        "LLMHUB_WEIGHTS_SECRET": WEIGHTS_SECRET.decode(),
    }
    cfg = weights_cfg(tmp_path)
    s = load_instance_secrets(cfg, env)
    assert s.instance == INSTANCE_SECRET and s.hf_token is None
    cfg2 = weights_cfg(tmp_path, hf_token_env="HF_READ_TOKEN")
    with pytest.raises(ConfigError) as e:
        load_instance_secrets(cfg2, env)
    assert "HF_READ_TOKEN" in str(e.value)
    s2 = load_instance_secrets(cfg2, env | {"HF_READ_TOKEN": HF_SECRET})
    assert s2.hf_token == HF_SECRET


def test_make_material_fns_reads_ca_and_skips_pools_without_weights(tmp_path: Path) -> None:
    cfg = weights_cfg(tmp_path)
    fns = make_material_fns(cfg, SECRETS)  # reads ca_pem_file from disk
    m = fns["p1"](inst_row(OP, T0))  # type: ignore[arg-type]
    assert (
        json.loads(m.env["LLMHUB_WEIGHTS_PLAN"])["sources"][0]["ca_pem"] == CA_PEMS["self_hosted"]
    )
    plain = cfg.model_copy(
        update={"pools": {"p1": cfg.pools["p1"].model_copy(update={"weights": None})}}
    )
    assert make_material_fns(plain, SECRETS) == {}
