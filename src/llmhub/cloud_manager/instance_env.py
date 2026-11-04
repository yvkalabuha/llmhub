"""Per-instance pod environment and secrets (T-370 stream E, plan sections 3 and 5.2).

Everything a pod needs is DERIVED from two controller secrets and the instance row, so a
restarted controller (or a repeated create with the same label) produces the same material
and nothing secret is ever stored:
- API key  = b64url(HMAC(instance_secret, "api:" + op_id));
- TLS      = Ed25519 key from HMAC(instance_secret, "tls:" + op_id), self-signed certificate
             with validity and serial derived from `created_at` and `op_id` (Ed25519 signs
             deterministically, so the DER and its sha256 fingerprint are stable);
- weights token = `issue_token(weights_secret, op_id, exp)`, exp = min(end of LOADING_MODEL,
  created_at + weights.token_ttl_max_s) (T-372 critic F9: <= 30 min after the row).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from llmhub.configuration.loader import (
    ConfigError,
    load_instance_secret,
    load_weights_ca,
    load_weights_secret,
)
from llmhub.configuration.models import Config, PoolConfig
from llmhub.weights.tokens import issue_token, token_expiry

TLS_NOT_BEFORE_SLACK_S = 3600  # pod clocks may lag a little
TLS_VALIDITY_S = 7 * 86400  # > max_lifetime_s (<= 3 h) with a wide margin
PLAN_VERSION = 1


@dataclass(frozen=True)
class InstanceSecrets:
    instance: bytes = field(repr=False)
    weights: bytes = field(repr=False)
    hf_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class InstanceMaterial:
    env: dict[str, str]
    secret_env: dict[str, str] = field(repr=False)
    tls_fingerprint: str | None


MaterialFn = Callable[[sqlite3.Row], InstanceMaterial]


def load_instance_secrets(cfg: Config, env: Mapping[str, str] = os.environ) -> InstanceSecrets:
    """Both secrets (>= 32 bytes) and, only if `weights.hf_token_env` is set, the HF token."""
    hf: str | None = None
    if cfg.weights.hf_token_env is not None:
        hf = env.get(cfg.weights.hf_token_env) or None
        if hf is None:
            raise ConfigError(f"{cfg.weights.hf_token_env} is not set (weights.hf_token_env)")
    instance, weights = load_instance_secret(env), load_weights_secret(env)
    if hmac.compare_digest(instance, weights):  # R8: the validator must not hold the API/TLS key
        raise ConfigError("LLMHUB_INSTANCE_SECRET and LLMHUB_WEIGHTS_SECRET must differ")
    return InstanceSecrets(instance=instance, weights=weights, hf_token=hf)


def _mac(secret: bytes, msg: str) -> bytes:
    return hmac.new(secret, msg.encode(), hashlib.sha256).digest()


def derive_api_key(instance_secret: bytes, op_id: str) -> str:
    return base64.urlsafe_b64encode(_mac(instance_secret, "api:" + op_id)).rstrip(b"=").decode()


def derive_tls(instance_secret: bytes, op_id: str, created_at: float) -> tuple[str, str, str]:
    """(cert_pem, key_pem, sha256(DER) hex). Deterministic for (secret, op_id, created_at)."""
    key = ed25519.Ed25519PrivateKey.from_private_bytes(_mac(instance_secret, "tls:" + op_id))
    start = dt.datetime.fromtimestamp(int(created_at) - TLS_NOT_BEFORE_SLACK_S, dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"llmhub-{op_id}")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(int(op_id, 16) + 1)
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(seconds=TLS_NOT_BEFORE_SLACK_S + TLS_VALIDITY_S))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, None)
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return cert_pem, key_pem, hashlib.sha256(der).hexdigest()


def weights_plan(
    op_id: str, created_at: float, pool: PoolConfig, cfg: Config, ca_pems: Mapping[str, str]
) -> dict[str, Any]:
    """`LLMHUB_WEIGHTS_PLAN` (version 1, plan section 3). No secrets inside."""
    if pool.weights is None:
        raise ConfigError(f"pools.{pool.name}.weights: no weight set")
    w = cfg.weights
    wset = w.sets[pool.weights]
    files = sorted(wset.files, key=lambda f: (f.id != "draft", f.id))  # small draft first
    ids = [f.id for f in files]
    sources: list[dict[str, Any]] = []
    for src in w.sources:
        if src.kind == "self_hosted" and src.name not in ca_pems:
            raise ConfigError(f"weights.sources.{src.name}: pinned certificate not loaded")
        sources.append(
            {
                "name": src.name,
                "base_url": src.base_url.replace("{op_id}", op_id),
                "paths": {fid: src.paths[fid] for fid in ids},
                "ca_pem": ca_pems.get(src.name) if src.kind == "self_hosted" else None,
                "auth": "weights_token" if src.kind == "self_hosted" else "none",
                "min_speed_mb_s": src.min_speed_mb_s,
                "segments": src.effective_segments,
            }
        )
    return {
        "version": PLAN_VERSION,
        "files": [{"id": f.id, "size": f.size, "sha256": f.sha256} for f in files],
        "sources": sources,
        "deadline_unix": token_expiry(created_at, cfg.timeouts),
        "speed_grace_s": w.speed_grace_s,
        "speed_window_s": w.speed_window_s,
        "read_timeout_s": w.read_timeout_s,
        "connect_timeout_s": w.connect_timeout_s,
        "retries_per_source": w.retries_per_source,
    }


def weights_token_exp(created_at: float, deadline_unix: int, cfg: Config) -> int:
    """F9: the token never outlives `token_ttl_max_s` after the row was created. Derived from
    the row (not the wall clock), so a repeated create gets the same token."""
    return min(deadline_unix, int(created_at) + cfg.weights.token_ttl_max_s)


def build_material(
    inst: sqlite3.Row | Mapping[str, Any],
    pool: PoolConfig,
    cfg: Config,
    secrets: InstanceSecrets,
    ca_pems: Mapping[str, str],
) -> InstanceMaterial:
    op_id = str(inst["op_id"])
    created_at = float(inst["created_at"])
    infer = pool.infer
    plan = weights_plan(op_id, created_at, pool, cfg, ca_pems)
    env = {
        "LLMHUB_OP_ID": op_id,
        "LLMHUB_TRANSPORT": infer.transport,
        "LLMHUB_PORT": str(infer.effective_port),
        "LLMHUB_WEIGHTS_PLAN": json.dumps(plan, sort_keys=True, separators=(",", ":")),
        "LLMHUB_LLAMA_ARGS": json.dumps(infer.llama_args),
        "LLMHUB_MAX_LIFETIME_S": str(cfg.timeouts.max_lifetime_s),
        "LLMHUB_WEIGHTS_DIR": infer.weights_dir,
    }
    secret_env = {"LLMHUB_API_KEY": derive_api_key(secrets.instance, op_id)}
    if any(s["auth"] == "weights_token" for s in plan["sources"]):
        secret_env["LLMHUB_WEIGHTS_TOKEN"] = issue_token(
            secrets.weights, op_id, weights_token_exp(created_at, int(plan["deadline_unix"]), cfg)
        )
    fingerprint: str | None = None
    if infer.transport == "direct":
        cert_pem, key_pem, fingerprint = derive_tls(secrets.instance, op_id, created_at)
        secret_env["LLMHUB_TLS_CERT_PEM"] = cert_pem
        secret_env["LLMHUB_TLS_KEY_PEM"] = key_pem
    if cfg.weights.hf_token_env is not None and secrets.hf_token:
        secret_env["HF_TOKEN"] = secrets.hf_token
    return InstanceMaterial(env=env, secret_env=secret_env, tls_fingerprint=fingerprint)


def make_material_fns(
    cfg: Config, secrets: InstanceSecrets, ca_pems: Mapping[str, str] | None = None
) -> dict[str, MaterialFn]:
    """Pool name -> material function, for every pool that has a weight set.

    Composition root helper: `Controller(..., materials=make_material_fns(cfg, secrets))`.
    The pinned certificates are read once here (`load_weights_ca`).
    """
    pems = dict(ca_pems) if ca_pems is not None else load_weights_ca(cfg)
    out: dict[str, MaterialFn] = {}
    for name, pool in cfg.pools.items():
        if pool.weights is None:
            continue

        def fn(inst: sqlite3.Row, _pool: PoolConfig = pool) -> InstanceMaterial:
            return build_material(inst, _pool, cfg, secrets, pems)

        out[name] = fn
    return out
