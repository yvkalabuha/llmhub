"""T-371 security review, cycle 1 (deploy safety): confirmed defects of the server script,
the run-args and the weights nginx config.

The findings were fixed in T-371 fix round 1; the former `xfail(strict=True)` markers are
removed and every test is a regression guard now. Docker tests use only local pinned images, never pull, run
with `--network none` and name their containers `llmhub-test-rev-*`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/server/llmhub-deploy.sh"
ARGS = ROOT / "deploy/docker/run-args"
NGINX_TEMPLATE = ROOT / "deploy/nginx-weights.conf"
F5_TEST = ROOT / "tests/test_t371_deploy_script.py"
IMAGES = dict(
    line.split("=", 1)
    for line in (ROOT / "deploy/docker/images.env").read_text().splitlines()
    if line and not line.startswith("#")
)


def _function(name: str) -> str:
    """The text of one shell function of the deploy script (from `name() {` to `}`)."""
    text = SCRIPT.read_text()
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.DOTALL | re.MULTILINE)
    assert m, name
    return m.group(0)


def _image_ok(ref: str) -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", ref], check=False, capture_output=True)
    return r.returncode == 0


def _rm(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, timeout=60)


# ------------------------------------------------------------- SEC-D1: secret in grep argv


def test_env_check_keeps_secret_out_of_argv(tmp_path: Path) -> None:
    secret = "S3cr3t-" + uuid.uuid4().hex
    etc = tmp_path / "etc"
    (etc / "secrets").mkdir(parents=True)
    (etc / "secrets" / "label").write_text(secret)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    real_grep = shutil.which("grep")
    assert real_grep
    log = tmp_path / "argv.log"
    wrapper = bindir / "grep"
    wrapper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >>"{log}"\nexec {real_grep} "$@"\n')
    wrapper.chmod(0o755)
    prog = (
        "set -u\n"
        "fail() { return 1; }\n"
        "docker() { printf '%s\\n' '[\"PATH=/usr/bin\",\"LLMHUB_LABEL_SECRET_FILE=/run/s/label\"]'; }\n"
        f"ETC={etc}\n" + _function("env_has_no_secret") + "\nenv_has_no_secret llmhub-api\n"
    )
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", prog], env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert log.exists(), "grep wrapper was not used"
    assert secret not in log.read_text()


# --------------------------------- SEC-D2: config replaced by rename, bind mount stays stale


def _api_config_mount() -> tuple[str, str, str]:
    """(host source, container dest, --config path) of llmhub-api from its run-args."""
    lines = [
        ln.strip()
        for ln in (ARGS / "llmhub-api.args").read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    cfg = lines[lines.index("--config") + 1]
    for i, ln in enumerate(lines):
        if ln == "-v":
            src, dest = lines[i + 1].split(":")[:2]
            if cfg == dest or cfg.startswith(dest.rstrip("/") + "/"):
                return src, dest, cfg
    raise AssertionError("no mount carries --config")


UBUNTU = IMAGES["TEST_SERVER_IMAGE"]


@pytest.mark.docker
@pytest.mark.skipif(not _image_ok(UBUNTU), reason=f"{UBUNTU} not available locally")
def test_new_config_is_visible_to_running_api(tmp_path: Path) -> None:
    src, dest, cfg = _api_config_mount()
    assert src.startswith("/etc/llmhub")
    etc = tmp_path / "etc-llmhub"
    host_src = etc / src.removeprefix("/etc/llmhub").lstrip("/")
    body = _function("install_config")
    m = re.search(r'new="\$ETC/([^"]+)"', body)
    c = re.search(r'cur="\$ETC/([^"]+)"', body)
    assert m and c
    new, cur = etc / m.group(1), etc / c.group(1)
    cur.parent.mkdir(parents=True, exist_ok=True)
    cur.write_text("clients: {old: 1}\n")
    host_src.parent.mkdir(parents=True, exist_ok=True)
    name = f"llmhub-test-rev-bind-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", name, "--network", "none", "--label",
             "llmhub-test=1", "-v", f"{host_src}:{dest}:ro", UBUNTU, "sleep", "120"],
            check=True, capture_output=True, timeout=60,
        )  # fmt: skip
        new.write_text("clients: {new: 1}\n")
        prog = f"set -eu\nchown() {{ :; }}\nETC={etc}\nUID_API=64100\n{body}\ninstall_config\n"
        subprocess.run(["bash", "-c", prog], check=True, capture_output=True, timeout=30)
        seen = subprocess.run(
            ["docker", "exec", name, "cat", cfg], check=True, capture_output=True, text=True
        ).stdout
        assert "new: 1" in seen, f"running container still sees: {seen!r}"
    finally:
        _rm(name)


# --------------------------------------- SEC-D3: global download cap is never accounted


def _w_all_key() -> str:
    m = re.search(r"limit_conn_zone\s+(\S+)\s+zone=w_all:", NGINX_TEMPLATE.read_text())
    assert m
    return m.group(1)


def _server_name_lines() -> list[str]:
    return re.findall(r"^\s*server_name\s+[^;]+;", NGINX_TEMPLATE.read_text(), re.MULTILINE)


def test_w_all_key_is_never_empty() -> None:
    key = _w_all_key()
    if key == "$server_name":
        assert any(ln.split()[1] not in ('""', "''") for ln in _server_name_lines())
    else:
        assert key in ("$server_port", "$server_addr") or not key.startswith("$")


NGINX = IMAGES["NGINX_IMAGE"]
PROBE = (
    "sleep 1; wget -q -O /dev/null http://127.0.0.1:9443/f & sleep 1;"
    " wget -S -O /dev/null http://127.0.0.1:9443/f 2>&1 | grep -m1 HTTP/"
)


@pytest.mark.docker
@pytest.mark.skipif(not _image_ok(NGINX), reason=f"{NGINX} not available locally")
def test_second_concurrent_download_is_refused(tmp_path: Path) -> None:
    conf = tmp_path / "n.conf"
    conf.write_text(
        "pid /tmp/nginx.pid;\nevents {}\nhttp {\n"
        f"  limit_conn_zone {_w_all_key()} zone=w_all:1m;\n"
        "  server {\n    listen 127.0.0.1:9443;\n"
        + "".join(f"    {ln.strip()}\n" for ln in _server_name_lines())
        + "    location /f { limit_conn w_all 1; limit_conn_status 503; limit_rate 20k;"
        " alias /tmp/big; }\n  }\n}\n"
    )
    conf.chmod(0o644)
    name = f"llmhub-test-rev-ngx-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", name, "--network", "none", "--label",
             "llmhub-test=1", "-v", f"{conf}:/etc/n.conf:ro", "--entrypoint", "sh", NGINX,
             "-c", "head -c 400000 /dev/zero >/tmp/big; nginx -c /etc/n.conf; sleep 60"],
            check=True, capture_output=True, timeout=60,
        )  # fmt: skip
        r = subprocess.run(
            ["docker", "exec", name, "sh", "-c", PROBE],
            check=False, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        assert " 503 " in r.stdout, r.stdout + r.stderr
    finally:
        _rm(name)


# ----------------------------------- SEC-D4: F5 "server" container can route to 10.8.0.1


def test_f5_server_container_has_no_network() -> None:
    text = F5_TEST.read_text()
    runs = re.findall(r'_run\("docker", "run", "-d".*?TEST_SERVER_IMAGE', text, re.DOTALL)
    assert runs
    for run in runs:
        assert '"--network", "none"' in run


# ------------------------------- SEC-D5: rollback removes the nginx image by shared Id


def test_rollback_s3_untags_our_tags_only() -> None:
    body = _function("rollback_s3")
    assert "llmhub-nginx:" in body and 'image rm "$img"' not in body
