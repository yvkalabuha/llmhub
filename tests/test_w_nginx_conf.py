"""D3/D4/D5: cert generator, nginx template statics, `nginx -t` and a real nginx e2e (docker)."""

from __future__ import annotations

import http.client
import importlib.util
import ipaddress
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from starlette.testclient import TestClient

from llmhub.clock import SystemClock
from llmhub.persistence.db import connect, migrate
from llmhub.weights.acct import AcctState
from llmhub.weights.auth_app import build_auth_app
from llmhub.weights.tokens import issue_token

ROOT = Path(__file__).resolve().parent.parent
CONF = (ROOT / "deploy" / "nginx-weights.conf").read_text(encoding="utf-8")
# the image pinned for the server (deploy/docker/images.env), never pulled by tests
IMAGE = "nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
SECRET = b"n" * 32


def _load_gen() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gen_weights_cert", ROOT / "deploy/gen_weights_cert.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_weights_cert"] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load_gen()


# ---------------------------------------------------------------- D3: certificate generator


def test_cert_generator_fields_and_key_mode(tmp_path: Path) -> None:
    fp = gen.generate("127.0.0.1", tmp_path)
    cert = x509.load_pem_x509_certificate((tmp_path / "weights-cert.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is False
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.value.digital_signature and not ku.value.key_cert_sign
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days >= 3650
    import hashlib

    assert (
        fp
        == hashlib.sha256(
            ssl.PEM_cert_to_DER_cert((tmp_path / "weights-cert.pem").read_text())
        ).hexdigest()
    )
    assert stat.S_IMODE((tmp_path / "weights-key.pem").stat().st_mode) == 0o600


def test_cert_generator_cli_rejects_bad_ip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gen.main(["not-an-ip", str(tmp_path)]) == 2
    assert gen.main([]) == 2


def test_cert_pins_in_python_tls_client(tmp_path: Path) -> None:
    gen.generate("127.0.0.1", tmp_path)
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(tmp_path / "weights-cert.pem", tmp_path / "weights-key.pem")
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    port = lsock.getsockname()[1]

    def serve() -> None:
        conn, _ = lsock.accept()
        with srv_ctx.wrap_socket(conn, server_side=True) as s:
            s.sendall(b"hi")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ctx = ssl.create_default_context(cadata=(tmp_path / "weights-cert.pem").read_text())
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        ctx.wrap_socket(raw, server_hostname="127.0.0.1") as s,
    ):
        assert s.recv(2) == b"hi"
    t.join(5)
    lsock.close()


# ---------------------------------------------------------------- D4: template statics


def _no_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


CODE = _no_comments(CONF)


def test_listens_only_on_placeholder_public_ip() -> None:
    assert "0.0.0.0" not in CODE and "10.8.0.1" not in CODE
    assert re.findall(r"^\s*listen\s+(\S+)", CODE, re.MULTILINE) == ["__PUBLIC_IP__:__PORT__"]
    assert "ssl_protocols TLSv1.3;" in CODE and "server_tokens off;" in CODE


def test_log_formats_have_no_secrets() -> None:
    formats = re.findall(r"log_format\s+\w+\s+(.*?);", CODE, re.DOTALL)
    assert len(formats) == 2
    for f in formats:
        for bad in (
            "$request ",
            '$request"',
            "$args",
            "$http_authorization",
            "$request_uri",
            "$query_string",
            "$http_",
        ):
            assert bad not in f
    assert "$weights_op $body_bytes_sent $status" in CODE
    assert re.search(r"access_log\s+\S+weights-acct\.log acct if=\$acct_ok;", CODE)


def test_required_directives() -> None:
    for need in (
        "limit_conn w_all __MAX_DL__;",
        "limit_conn w_op __SEGMENTS__;",
        "limit_rate __RATE__;",
        "auth_request /_auth;",
        "disable_symlinks on;",
        "limit_except GET { deny all; }",
        "alias __MODELS__/$rel;",
        "proxy_pass http://__AUTH__/weights/auth;",
        "proxy_set_header X-Real-IP $remote_addr;",
        "proxy_set_header X-Range $http_range;",
        "location / { access_log off; return 404; }",
        "proxy_set_header X-If-Range $http_if_range;",
        "max_ranges 1;",
        "worker_processes 1;",
        "worker_priority 10;",
        "pid __PID__;",
        "directio 4m;",
        "output_buffers 2 1m;",
        "sendfile on;",
    ):
        assert need in CODE, need


def _location_regex() -> re.Pattern[str]:
    m = re.search(r'location ~ "(.*?)" \{', CODE)
    assert m
    return re.compile(m.group(1).replace("(?<rel>", "(?P<rel>"))


OP = "0123456789ab"


@pytest.mark.parametrize(
    "path",
    [
        f"/w/{OP}/qwen/model.gguf",
        f"/w/{OP}/qwen-3.6/Model_Q4.gguf",
    ],
)
def test_location_regex_accepts(path: str) -> None:
    assert _location_regex().fullmatch(path)


@pytest.mark.parametrize(
    "path",
    [
        f"/w/{OP}/../x.gguf",
        f"/w/{OP}/a/../../etc/passwd",
        f"/w/{OP}/a/../../x.gguf",
        f"/w/{OP}/x.txt",
        f"/w/{OP}/qwen/x.txt",
        f"/w/{OP}/qwen/.gguf",
        f"/w/{OP}/qwen/..gguf",
        f"/w/{OP}/.hidden/x.gguf",
        f"/w/{OP}/a/b/x.gguf",
        f"/w/{OP}//qwen/x.gguf",
        f"/w/{OP}/qwen/x.gguf\n",
        "/w/NOTHEXNOTHEX/qwen/x.gguf",
        "/w/0123456789a/qwen/x.gguf",
        "/w/0123456789abc/qwen/x.gguf",
        "/qwen/x.gguf",
    ],
)
def test_location_regex_rejects(path: str) -> None:
    assert not _location_regex().fullmatch(path)


# ---------------------------------------------------------------- docker helpers


def _docker_ok() -> str | None:
    if shutil.which("docker") is None:
        return "docker binary not found"
    r = subprocess.run(["docker", "image", "inspect", IMAGE], check=False, capture_output=True)
    if r.returncode != 0:
        return f"image {IMAGE} not available locally (tests never pull)"
    return None


_REASON = _docker_ok()
docker = pytest.mark.docker
needs_docker = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _render(
    public_ip: str, port: int, *, rate: str = "80m", segments: int = 1,
    auth: str = "127.0.0.1:8096", max_dl: int | None = None,
) -> str:
    c = CONF
    for k, v in {
        "__PUBLIC_IP__": public_ip, "__PORT__": str(port), "__RATE__": rate,
        "__SEGMENTS__": str(segments), "__MAX_DL__": str(max_dl or segments),
        "__AUTH__": auth, "__MODELS__": "/models",
        "__LOGDIR__": "/work/log", "__CERT__": "/work/cert/weights-cert.pem",
        "__KEY__": "/work/cert/weights-key.pem", "__PID__": "/work/nginx.pid",
        "__TMP__": "/work/tmp",
    }.items():  # fmt: skip
        c = c.replace(k, v)
    assert not re.search(r"__[A-Z]+__", _no_comments(c))
    return c


def _workdir(tmp: Path) -> Path:
    w = tmp / "work"
    for d in ("log", "cert", "tmp"):
        (w / d).mkdir(parents=True)
    gen.generate("127.0.0.1", w / "cert")
    (w / "cert").chmod(0o755)
    (w / "cert" / "weights-key.pem").chmod(0o644)  # container user = host uid, still fine
    return w


def _run_nginx(args: list[str], w: Path, models: Path, extra: list[str] | None = None) -> list[str]:
    return [
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{w}:/work", "-v", f"{models}:/models:ro", *(extra or []), IMAGE, *args,
    ]  # fmt: skip


@docker
@needs_docker
def test_nginx_syntax_ok(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    models = tmp_path / "models"
    models.mkdir()
    (w / "weights.conf").write_text(_render("127.0.0.1", 9443))
    r = subprocess.run(
        _run_nginx(["nginx", "-t", "-c", "/work/weights.conf"], w, models),
        check=False, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- D5: real nginx e2e

MODEL = bytes(range(256)) * 12  # 3072 bytes
DRAFT = b"d" * 128
BIG = os.urandom(96 * 1024)


class Rig:
    def __init__(self) -> None:
        self.port = 0
        self.op = OP
        self.conn: Any = None
        self.app: Any = None
        self.tls: ssl.SSLContext
        self.w: Path
        self.token = ""
        self.name = ""

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=f"https://127.0.0.1:{self.port}", verify=self.tls, timeout=20)

    def headers(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def raw(self, target: str, token: str | None = None, method: str = "GET") -> tuple[int, bytes]:
        c = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.tls, timeout=20)
        try:
            c.request(method, target, headers=self.headers(token))
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    stops: list[Callable[[], None]] = []

    def make(rate: str = "80m", segments: int = 1) -> Rig:
        r = Rig()
        w = _workdir(tmp_path)
        r.w = w
        models = tmp_path / "models"
        (models / "qwen").mkdir(parents=True)
        (models / "qwen" / "model.gguf").write_bytes(MODEL)
        (models / "qwen" / "draft.gguf").write_bytes(DRAFT)
        (models / "qwen" / "big.gguf").write_bytes(BIG)
        (models / "qwen" / "model.txt").write_bytes(b"txt")
        (models / "qwen" / "link.gguf").symlink_to("/etc/passwd")
        outside = tmp_path / "secret.gguf"
        outside.write_bytes(b"OUTSIDE")
        r.tls = ssl.create_default_context(cadata=(w / "cert" / "weights-cert.pem").read_text())
        # controller DB with one loading instance
        db = tmp_path / "state.db"
        r.conn = connect(db)
        migrate(r.conn)
        r.conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
            " created_at, max_lifetime_at) VALUES ('inst-a', ?, 'p', 'mock', 'l', 'LOADING_MODEL',"
            " 0, 0, 1e12)", (OP,)
        )  # fmt: skip
        r.token = issue_token(SECRET, OP, int(time.time()) + 3600)
        # validator (uvicorn in a thread)
        sizes = {
            "qwen/model.gguf": (len(MODEL), len(MODEL) + len(DRAFT)),
            "qwen/draft.gguf": (len(DRAFT), len(MODEL) + len(DRAFT)),
            "qwen/big.gguf": (len(BIG), 10 * len(BIG)),
            "qwen/link.gguf": (100, 1000),
        }
        (w / "log" / "weights-acct.log").touch()
        r.app = build_auth_app(
            db, SECRET, frozenset(sizes), SystemClock(),
            acct=AcctState(tmp_path / "auth", w / "log" / "weights-acct.log"), sizes=sizes,
        )  # fmt: skip
        aport = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(r.app, host="127.0.0.1", port=aport, log_level="critical")
        )
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        # nginx
        r.port = _free_port()
        (w / "weights.conf").write_text(
            _render("127.0.0.1", r.port, rate=rate, segments=segments, auth=f"127.0.0.1:{aport}")
        )
        name = f"llmhub-test-w-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            _run_nginx(["nginx", "-c", "/work/weights.conf", "-g", "daemon off;"], w, models,
                       ["-d", "--name", name, "--network", "host"]),
            check=True, capture_output=True, timeout=60,
        )  # fmt: skip
        r.name = name

        def stop() -> None:
            subprocess.run(
                ["docker", "rm", "-f", name], check=False, capture_output=True, timeout=60
            )
            server.should_exit = True
            th.join(5)
            r.conn.close()

        stops.append(stop)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", r.port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            logs = subprocess.run(
                ["docker", "logs", name], check=False, capture_output=True, text=True
            )
            raise AssertionError(f"nginx did not start: {logs.stderr}")
        return r

    yield make
    for s in stops:
        s()


def _wait_log(path: Path, needle: str, timeout: float = 5.0) -> str:
    end = time.time() + timeout
    while time.time() < end:
        txt = path.read_text()
        if needle in txt:
            return txt
        time.sleep(0.05)
    return path.read_text()


@docker
@needs_docker
def test_e2e_download_range_and_revocation(rig: Callable[..., Rig]) -> None:
    r = rig()
    with r.client() as c:
        full = c.get(f"/w/{OP}/qwen/model.gguf", headers=r.headers())
        assert full.status_code == 200 and full.content == MODEL
        part = c.get(f"/w/{OP}/qwen/draft.gguf", headers={**r.headers(), "Range": "bytes=10-49"})
        assert part.status_code == 206 and part.content == DRAFT[10:50]
        assert c.get(f"/w/{OP}/qwen/draft.gguf").status_code == 401
        # instance reached READY -> the same resume is refused (pod fails over to HF)
        r.conn.execute("UPDATE instances SET state='READY'")
        assert c.get(f"/w/{OP}/qwen/draft.gguf", headers=r.headers()).status_code == 403


@docker
@needs_docker
def test_e2e_method_and_extension(rig: Callable[..., Rig]) -> None:
    r = rig()
    assert r.raw(f"/w/{OP}/qwen/model.gguf", method="POST")[0] in (403, 405)
    assert r.raw(f"/w/{OP}/qwen/model.gguf", method="DELETE")[0] in (403, 405)
    assert r.raw(f"/w/{OP}/qwen/model.txt")[0] == 404
    assert r.raw("/")[0] == 404 and r.raw("/_auth")[0] in (404, 403)
    assert r.raw(f"/w/{OP}/qwen/")[0] == 404


@docker
@needs_docker
def test_e2e_symlink_not_followed(rig: Callable[..., Rig]) -> None:
    r = rig()
    status, body = r.raw(f"/w/{OP}/qwen/link.gguf")  # allowed by validator, symlink to passwd
    assert status in (403, 404) and b"root:" not in body


@docker
@needs_docker
@pytest.mark.parametrize(
    "target",
    [
        "/w/{op}/%2e%2e/%2e%2e/etc/passwd",
        "/w/{op}//etc/passwd",
        "/w/{op}/qwen%2F..%2Fx.gguf",
        "/w/{op}/a/../../x.gguf",
        "/w/{op}/qwen/%2e%2e/%2e%2e/secret.gguf",
        "/w/{op}/qwen/..%2f..%2fsecret.gguf",
        "/w/{op}/../w/{op}/qwen/model.gguf",
    ],
)
def test_e2e_path_tricks(rig: Callable[..., Rig], target: str) -> None:
    r = rig()
    status, body = r.raw(target.format(op=OP))
    assert status in (400, 403, 404), (target, status)
    assert b"root:" not in body and b"OUTSIDE" not in body


@docker
@needs_docker
def test_e2e_second_parallel_connection_gets_503(rig: Callable[..., Rig]) -> None:
    r = rig(rate="1k", segments=1)
    with r.client() as c1, r.client() as c2:  # noqa: SIM117
        with c1.stream("GET", f"/w/{OP}/qwen/big.gguf", headers=r.headers()) as resp:
            assert resp.status_code == 200
            it = resp.iter_raw(1024)  # keep a reference: a dropped generator closes the stream
            next(it)
            second = c2.get(f"/w/{OP}/qwen/draft.gguf", headers=r.headers())
            assert second.status_code == 503
    # slot is released afterwards
    time.sleep(0.5)
    with r.client() as c3:
        assert c3.get(f"/w/{OP}/qwen/draft.gguf", headers=r.headers()).status_code == 200


@docker
@needs_docker
def test_e2e_two_segments_allow_two_connections(rig: Callable[..., Rig]) -> None:
    r = rig(rate="1k", segments=2)
    with r.client() as c1, r.client() as c2:  # noqa: SIM117
        with c1.stream("GET", f"/w/{OP}/qwen/big.gguf", headers=r.headers()) as a:
            it = a.iter_raw(1024)
            next(it)
            with c2.stream("GET", f"/w/{OP}/qwen/big.gguf", headers=r.headers()) as b:
                assert b.status_code == 200
                with r.client() as c3:
                    assert (
                        c3.get(f"/w/{OP}/qwen/draft.gguf", headers=r.headers()).status_code == 503
                    )


@docker
@needs_docker
def test_e2e_volume_cap_other_ip_and_log_hygiene(rig: Callable[..., Rig]) -> None:
    r = rig()
    acct = r.w / "log" / "weights-acct.log"
    with r.client() as c:
        assert c.get(f"/w/{OP}/qwen/model.gguf", headers=r.headers()).status_code == 200
        _wait_log(acct, f"{OP} ")
        # served 3072; cap = 1.2 * 3200 = 3840: a second whole model would exceed it
        assert c.get(f"/w/{OP}/qwen/model.gguf", headers=r.headers()).status_code == 403
        # but a small resume still fits
        ok = c.get(f"/w/{OP}/qwen/draft.gguf", headers={**r.headers(), "Range": "bytes=0-99"})
        assert ok.status_code == 206
    # another client address: first IP (127.0.0.1) is bound; a different X-Real-IP is refused
    direct = TestClient(r.app, client=("127.0.0.1", 1))
    h = {
        "X-Original-URI": f"/w/{OP}/qwen/draft.gguf",
        "Authorization": f"Bearer {r.token}",
        "X-Real-IP": "203.0.113.9",
    }
    assert direct.get("/weights/auth", headers=h).status_code == 403
    h["X-Real-IP"] = "127.0.0.1"
    assert direct.get("/weights/auth", headers=h).status_code == 204
    # logs: the token never appears anywhere nginx writes
    texts = [p.read_text() for p in (r.w / "log").iterdir()]
    texts.append(
        subprocess.run(
            ["docker", "logs", r.name], check=False, capture_output=True, text=True
        ).stderr
    )
    blob = "\n".join(texts)
    assert r.token not in blob and r.token.split(".")[3] not in blob
    assert "Bearer" not in blob and "?" not in (r.w / "log" / "weights-access.log").read_text()
    assert f"op={OP}" in blob
