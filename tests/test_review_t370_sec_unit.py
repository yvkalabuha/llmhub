"""T-370 security review, cycle 1: unit-level probes (no network, no docker).

Tests of an OPEN finding are `xfail(strict=True)` with the finding id: they turn XPASS (and
fail the run) once the fix lands, so the marker goes away together with the fix.

Fix round 1: all findings covered here are fixed; the xfail markers were removed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
from pathlib import Path

import httpx
import pytest
from llmhub_infer.front import PodState, build_app
from llmhub_infer.supervisor import EnvError, check_llama_args, child_env, read_env
from starlette.testclient import TestClient

from llmhub.clock import FakeClock
from llmhub.cloud_manager.instance_env import (
    InstanceMaterial,
    InstanceSecrets,
    derive_api_key,
    derive_tls,
    load_instance_secrets,
)
from llmhub.configuration.loader import ConfigError
from llmhub.persistence.db import connect, migrate
from llmhub.weights.acct import AcctState
from llmhub.weights.auth_app import build_auth_app, requested_bytes
from llmhub.weights.tokens import Verdict, check_request, issue_token

OP = "0123456789ab"
S_INST = b"i" * 32
S_W = b"w" * 32


# ------------------------------------------------------------------ KDF / domain separation


def test_api_key_tls_seed_and_token_are_domain_separated() -> None:
    """Same secret bytes in both roles must still give unrelated outputs."""
    api = derive_api_key(S_INST, OP)
    tok_sig = issue_token(S_INST, OP, 10**9).split(".")[3]
    tls_seed = hmac.new(S_INST, f"tls:{OP}".encode(), hashlib.sha256).digest()
    assert api != tok_sig
    assert base64.urlsafe_b64decode(api + "=") != tls_seed
    assert len(api) == 43  # 256-bit key
    assert derive_api_key(S_INST, OP) != derive_api_key(S_INST, "ba9876543210")
    assert derive_api_key(S_INST, OP) != derive_api_key(b"j" * 32, OP)


def test_tls_material_differs_per_op_and_secret() -> None:
    a = derive_tls(S_INST, OP, 1000.0)
    assert derive_tls(S_INST, "ba9876543210", 1000.0)[2] != a[2]
    assert derive_tls(b"j" * 32, OP, 1000.0)[1] != a[1]
    assert "PRIVATE KEY" in a[1] and "PRIVATE" not in a[0]


def test_instance_and_weights_secret_must_differ() -> None:
    env = {"LLMHUB_INSTANCE_SECRET": "x" * 32, "LLMHUB_WEIGHTS_SECRET": "x" * 32}

    class _W:
        hf_token_env = None

    class _C:
        weights = _W()

    with pytest.raises(ConfigError):
        load_instance_secrets(_C(), env)  # type: ignore[arg-type]


# ------------------------------------------------------------------ repr / message hygiene


def test_material_and_secrets_repr_hide_values() -> None:
    m = InstanceMaterial(
        env={"A": "1"}, secret_env={"LLMHUB_API_KEY": "SEKRIT"}, tls_fingerprint=None
    )
    s = InstanceSecrets(instance=b"SEKRIT-I" * 4, weights=b"SEKRIT-W" * 4, hf_token="hf_SEKRIT")
    assert "SEKRIT" not in repr(m) and "SEKRIT" not in repr(s) and "SEKRIT" not in str(s)


def test_pod_env_errors_never_echo_values() -> None:
    bad = {
        "LLMHUB_OP_ID": OP, "LLMHUB_TRANSPORT": "direct", "LLMHUB_API_KEY": "SEKRIT-KEY",
        "LLMHUB_WEIGHTS_PLAN": '{"version": 1, "x": "SEKRIT-PLAN"}',
        "LLMHUB_TLS_KEY_PEM": "SEKRIT-PEM",
    }  # fmt: skip
    with pytest.raises(EnvError) as ei:
        read_env(bad)
    assert "SEKRIT" not in str(ei.value)
    with pytest.raises(EnvError) as ei2:
        check_llama_args(["--api-key", "SEKRIT-ARG"])
    assert "SEKRIT" not in str(ei2.value)


# ------------------------------------------------------------------ pod env / llama-server


def test_child_env_strips_common_secret_names() -> None:
    env = {
        "LLMHUB_API_KEY": "x", "LLMHUB_WEIGHTS_TOKEN": "x", "HF_TOKEN": "x",
        "RUNPOD_API_KEY": "x", "AWS_SECRET_ACCESS_KEY": "x", "JUPYTER_PASSWORD": "x",
        "LLAMA_ARG_API_KEY": "x", "GH_PAT_CREDENTIALS": "x", "PATH": "/bin",
    }  # fmt: skip
    assert child_env(env) == {"PATH": "/bin"}


def test_child_env_drops_llama_arg_overrides() -> None:
    out = child_env({"LLAMA_ARG_HOST": "0.0.0.0", "LLAMA_ARG_PORT": "8080"})
    assert "LLAMA_ARG_HOST" not in out and "LLAMA_ARG_PORT" not in out


@pytest.mark.parametrize(
    "args",
    [["-hf", "a/b"], ["--hf-repo", "a/b"], ["-hft", "t"], ["-mu", "https://x/y.gguf"],
     ["--path", "/"], ["--slot-save-path", "/tmp"], ["--props"]],
)  # fmt: skip
def test_llama_args_reject_model_fetch_and_file_flags(args: list[str]) -> None:
    with pytest.raises(EnvError):
        check_llama_args(args)


# ------------------------------------------------------------------ pod front


def _front(seen: list[bytes]) -> TestClient:
    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.raw_path)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

    st = PodState()
    st.set_phase("ready")
    return TestClient(build_app(st, "KEY-1", upstream_transport=httpx.MockTransport(h)))


@pytest.mark.parametrize(
    "hdr",
    [None, "", "Bearer", "Bearer ", "Basic KEY-1", "Bearer KEY-", "Bearer KEY-12",
     "Bearer key-1", "KEY-1"],
)  # fmt: skip
def test_front_key_variants_rejected(hdr: str | None) -> None:
    c = _front([])
    h = {} if hdr is None else {"Authorization": hdr}
    assert c.get("/health", headers=h).status_code == 401
    assert c.post("/v1/chat/completions", headers=h, content=b"{}").status_code == 401
    assert c.get("/ping").status_code == 204  # the only open route


def test_front_does_not_forward_authorization_upstream() -> None:
    got: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        got.append(req)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

    st = PodState()
    st.set_phase("ready")
    c = TestClient(build_app(st, "KEY-1", upstream_transport=httpx.MockTransport(h)))
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer KEY-1"}, content=b"{}")
    assert r.status_code == 200
    assert "authorization" not in {k.lower() for k in got[0].headers}


@pytest.mark.parametrize("path", ["/v1/%2e%2e/slots", "/v1/..%2Fslots", "/v1/%2E%2E/metrics"])
def test_front_proxy_stays_under_v1(path: str) -> None:
    seen: list[bytes] = []
    c = _front(seen)
    c.post(path, headers={"Authorization": "Bearer KEY-1"}, content=b"{}")
    assert all(p.startswith(b"/v1/") for p in seen)


# ------------------------------------------------------------------ validator


def test_token_from_other_secret_or_role_rejected() -> None:
    tok = issue_token(S_INST, OP, 10**10)  # signed with the INSTANCE secret
    v, _ = check_request(S_W, f"Bearer {tok}", f"/w/{OP}/q/m.gguf", 0, lambda _o: "LOADING_MODEL",
                         frozenset({"q/m.gguf"}))  # fmt: skip
    assert v is Verdict.BAD_SIG
    api = derive_api_key(S_W, OP)  # an API key is never a weights token
    v2, _ = check_request(S_W, f"Bearer {api}", f"/w/{OP}/q/m.gguf", 0, lambda _o: "LOADING_MODEL",
                          frozenset({"q/m.gguf"}))  # fmt: skip
    assert v2 is Verdict.MALFORMED


@pytest.mark.parametrize(
    "hdr",
    ["bytes=0-0,0-", "bytes=0-1, 2-3", "BYTES=0-0", "bytes = 0-0", "bytes=0-0 ", "items=0-0"],
)
def test_requested_bytes_unusual_ranges_charge_whole_file(hdr: str) -> None:
    want = 100 if hdr != "bytes=0-0 " else 1  # strip() is applied to the header
    assert requested_bytes(hdr, 100) == want


def test_validator_does_not_touch_db_or_acct_for_bad_tokens(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    acct = AcctState(tmp_path / "auth", tmp_path / "acct.log")
    app = build_auth_app(db, S_W, frozenset({"q/m.gguf"}), FakeClock(0.0), acct=acct,
                         sizes={"q/m.gguf": (10, 10)})  # fmt: skip
    c = TestClient(app)
    for auth in ("Bearer v1.0123456789ab.99999999999.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                 "Bearer " + "x" * 8000):  # fmt: skip
        h = {"X-Original-URI": f"/w/{OP}/q/m.gguf", "Authorization": auth, "X-Real-IP": "1.1.1.1"}
        assert c.get("/weights/auth", headers=h).status_code == 403
    with sqlite3.connect(tmp_path / "auth" / "auth-state.db") as s:
        assert s.execute("SELECT COUNT(*) FROM ip_bind").fetchone()[0] == 0


def test_metrics_refused_for_non_local_client(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    app = build_auth_app(db, S_W, frozenset(), FakeClock(0.0),
                         acct=AcctState(tmp_path / "a", tmp_path / "l"), sizes={})  # fmt: skip
    assert TestClient(app, client=("203.0.113.5", 1)).get("/metrics").status_code == 403
    body = TestClient(app, client=("127.0.0.1", 1)).get("/metrics").text
    assert "Bearer" not in body and OP not in body  # counters by verdict only
