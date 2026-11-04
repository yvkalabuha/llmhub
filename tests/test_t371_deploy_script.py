"""T-371 E4 (critic F5): `deploy/server/llmhub-deploy.sh` on a disposable "server".

The server is a privileged `ubuntu:22.04` container (same OS as 65.109.95.35) with
`--network none` (SEC-D4: no route to the real 10.8.0.1 through the workstation VPN) running its own
dockerd (static binaries from the pinned docker:dind image, `--iptables=false`, no bridge:
every llmhub container uses `--network host`). Inside it: a fake `/etc/llama/api-key`
(root:998 0640), `/opt/models` with two small GGUF files and a sha256 file, a ufw stub that
keeps rules in a file (pre-seeded with a foreign rule), `systemctl`/`logrotate` stubs and a
foreign container `searxng-fake`. API and weights bind 127.0.0.1 (LLMHUB_API_HOST /
LLMHUB_PUBLIC_IP).

For every step s2..s9: apply -> check 0 -> second apply changes nothing (file hashes/modes,
users, ufw rules, images, container Id/StartedAt) -> rollback -> check != 0 and no traces
(same view as before the step) -> apply again for the next step. Then a weights probe through
nginx + validator (token, Range, DB read via the read-only mount), the e2e client removal
(s10x), s11 against the s0 baseline, the Env of every container without secret values, the
foreign container unchanged, and the full rollback s14.

Deviation from a real server (documented in the task journal): the nested daemon has no
journald, so the dist copy used here swaps `--log-driver journald` for `local`.

Run: `uv run pytest -m docker tests/test_t371_deploy_script.py` (~5-10 min). Containers are
named `llmhub-test-*`, labelled `llmhub-test=1` and removed with their volumes at the end.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.slow]

ROOT = Path(__file__).resolve().parent.parent
IMAGES = dict(
    line.split("=", 1)
    for line in (ROOT / "deploy/docker/images.env").read_text().splitlines()
    if line and not line.startswith("#")
)
STEPS = ["s2", "s3", "s4", "s5", "s6", "s7", "s8a", "s8b", "s8c", "s9"]
MODEL = os.urandom(2 * 1024 * 1024)
DRAFT = os.urandom(64 * 1024)
DBIN = "/opt/dbin"
BACKENDS = ("qwen-35b-a3b", "qwen-27b", "qwen-9b")
NGINX_FOREIGN_TAG = "searxng-proxy:upstream"
REAL_ADDRS = ("10.8.0.1", "65.109.95.35")

UFW_STUB = r"""#!/bin/bash
# test stub of ufw: rules live in a file as "<spec> # <comment>"
f=/var/lib/ufw-stub/rules; mkdir -p "${f%/*}"; touch "$f"
case "$1" in
  status) i=0; while IFS= read -r l; do i=$((i+1));
            if [ "${2:-}" = numbered ]; then echo "[ $i] $l"; else echo "$l"; fi; done <"$f" ;;
  allow) shift; spec=(); c=""
         while [ $# -gt 0 ]; do if [ "$1" = comment ]; then c=$2; shift 2; else spec+=("$1"); shift; fi; done
         line="${spec[*]} # $c"; grep -qxF "$line" "$f" || echo "$line" >>"$f" ;;
  delete) shift; [ "$1" = allow ] && shift; { grep -vF "$* #" "$f" || true; } >"$f.tmp"; mv "$f.tmp" "$f" ;;
  *) echo "ufw stub: unsupported $1" >&2; exit 2 ;;
esac
"""

# what must be identical after a repeated apply (contents, modes, owners, Id/StartedAt)
STATE = r"""
set -u
for d in /etc/llmhub /var/lib/llmhub-deploy /etc/systemd/system /etc/logrotate.d; do
  [ -e "$d" ] && find "$d" -path /var/lib/llmhub-deploy/preflight/now -prune -o \
     -name 'config.yaml.bak-*' -prune -o -printf '%p %u:%g %m\n'
done | sort
# data dirs (state.db, validator state, accounting logs) stay on rollback: only the dirs
stat -c '%n %u:%g %a' /var/lib/llmhub /var/lib/llmhub/backup /var/lib/llmhub-weights-auth \
     /var/log/llmhub 2>/dev/null || true
find /etc/llmhub /var/lib/llmhub-deploy -type f ! -path '*/preflight/now/*' \
     ! -name 'config.yaml.bak-*' -print0 2>/dev/null | sort -z | xargs -0r sha256sum
getent passwd | grep llmhub || true; getent group | grep llmhub || true
cat /var/lib/ufw-stub/rules
docker images -q --no-trunc | sort -u
docker ps -a --filter name=llmhub- --format '{{.Names}}' | sort | while read -r n; do
  docker inspect -f "{{.Name}} {{.Image}} ${WITH_START:+{{.State.StartedAt}}}" "$n"; done
"""


@dataclass
class Server:
    name: str
    dist: str  # path inside the server
    env: dict[str, str]

    def sh(self, script: str, *, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "exec"]
        for k, v in self.env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.name, "bash", "-c", script]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if check and r.returncode != 0:
            raise AssertionError(f"{script!r} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def step(self, step: str, action: str) -> subprocess.CompletedProcess[str]:
        return self.sh(f"{self.dist}/llmhub-deploy.sh {step} {action}", check=False)

    def ok(self, step: str, action: str) -> None:
        r = self.step(step, action)
        assert r.returncode == 0, f"{step} {action}: {r.returncode}\n{r.stdout}\n{r.stderr}"

    def state(self, with_start: bool = True) -> str:
        return self.sh(f"WITH_START={'1' if with_start else ''}; {STATE}").stdout


def _need() -> str | None:
    if shutil.which("docker") is None:
        return "docker not found"
    for key in ("TEST_SERVER_IMAGE", "TEST_DIND_IMAGE", "PYTHON_IMAGE", "NGINX_IMAGE"):
        r = subprocess.run(["docker", "image", "inspect", IMAGES[key]], capture_output=True,
                           check=False)  # fmt: skip
        if r.returncode != 0:
            return f"{IMAGES[key]} not available locally (tests never pull)"
    return None


def _run(*cmd: str, timeout: int = 600) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout).stdout


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    reason = _need()
    if reason:
        pytest.skip(reason)
    tmp = tmp_path_factory.mktemp("f5")
    env = {**os.environ, "LLMHUB_DIST": str(tmp / "dist")}
    subprocess.run(["nice", "-n", "10", str(ROOT / "deploy/docker/build.sh")], env=env,
                   check=True, capture_output=True, timeout=900)  # fmt: skip
    (dist,) = (tmp / "dist").iterdir()
    ver = dist.name
    common = dist / "run-args" / "_common.args"
    common.write_text(common.read_text().replace("journald", "local"))  # no journald in dind
    (dist / "SHA256SUMS").unlink()
    sums = _run("bash", "-c", f"cd {dist} && find . -type f -printf '%P\\n' | LC_ALL=C sort"
                              " | xargs sha256sum")  # fmt: skip
    (dist / "SHA256SUMS").write_text(sums)
    # static docker binaries from the pinned dind image
    tag = uuid.uuid4().hex[:8]
    dbin = tmp / "dbin"
    cp = f"llmhub-test-dindbin-{tag}"
    _run("docker", "create", "--name", cp, "--label", "llmhub-test=1", IMAGES["TEST_DIND_IMAGE"])
    try:
        _run("docker", "cp", f"{cp}:/usr/local/bin/.", str(dbin))
    finally:
        subprocess.run(["docker", "rm", "-v", cp], capture_output=True, check=False)
    stub = tmp / "ufw"
    stub.write_text(UFW_STUB)
    stub.chmod(0o755)
    assets = tmp / "assets"
    (assets / "qwen").mkdir(parents=True)
    (assets / "qwen" / "model.gguf").write_bytes(MODEL)
    (assets / "qwen" / "draft.gguf").write_bytes(DRAFT)
    name = f"llmhub-test-srv-{tag}"
    # SEC-D4: no network at all (the workstation VPN routes to 10.8.0.1): images come in by
    # `docker save | docker exec -i`, every backend of the template is 127.0.0.1
    _run("docker", "run", "-d", "--name", name, "--network", "none", "--label", "llmhub-test=1",
         "--privileged", "-v", "/var/lib/docker", "-v", f"{dbin}:{DBIN}:ro", "-v", f"{dist}:/root/llmhub-dist/{ver}:ro",
         "-v", f"{stub}:/usr/local/sbin/ufw:ro", "-v", f"{assets}:/assets:ro",
         IMAGES["TEST_SERVER_IMAGE"], "sleep", "infinity")  # fmt: skip
    render_set = ";".join(
        ["api.host=127.0.0.1"] + [f"local_backends.{b}.url=http://127.0.0.1:1/v1" for b in BACKENDS]
    )
    srv = Server(name, f"/root/llmhub-dist/{ver}", {
        "PATH": f"{DBIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LLMHUB_UFW": "/usr/local/sbin/ufw", "LLMHUB_SYSTEMCTL": "true", "LLMHUB_LOGROTATE": "true",
        "LLMHUB_API_HOST": "127.0.0.1", "LLMHUB_PUBLIC_IP": "127.0.0.1",
        "LLMHUB_RENDER_SET": render_set, "LLMHUB_WEIGHTS_MODEL": "qwen/model.gguf",
        "LLMHUB_WEIGHTS_DRAFT": "qwen/draft.gguf", "LLMHUB_HEALTH_WAIT_S": "120",
    })  # fmt: skip
    try:
        srv.sh("(dind dockerd --iptables=false --ip6tables=false --bridge=none"
               " >/var/log/dockerd.log 2>&1 &) ; for i in $(seq 60); do docker info >/dev/null"
               " 2>&1 && exit 0; sleep 1; done; cat /var/log/dockerd.log; exit 1")  # fmt: skip
        srv.sh(
            "set -e; mkdir -p /etc/llama /opt/models /etc/systemd/system /etc/logrotate.d"
            "; head -c 40 /dev/urandom | base64 -w0 >/etc/llama/api-key"
            "; chown 0:998 /etc/llama/api-key; chmod 0640 /etc/llama/api-key"
            "; cp -r /assets/qwen /opt/models/; chmod -R a+rX /opt/models"
            "; (cd /opt/models && sha256sum /opt/models/qwen/*.gguf >sha256-T365.txt)"
            "; /usr/local/sbin/ufw allow 22/tcp comment 'ssh (foreign)'"
        )
        _run("bash", "-c", f"docker save {IMAGES['TEST_SERVER_IMAGE']} | docker exec -i {name} "
                           f"{DBIN}/docker load -q")  # fmt: skip
        # SEC-D5: the upstream nginx image (same Id as ours) is already there under a FOREIGN
        # tag; s3 must load our tag and rollback/s14 must leave the foreign tag alone
        foreign_tag = NGINX_FOREIGN_TAG
        _run("bash", "-c", f"docker save {IMAGES['NGINX_IMAGE']} | docker exec -i {name} "
                           f"{DBIN}/docker load -q")  # fmt: skip
        nginx_id = _run("docker", "image", "inspect", "-f", "{{.Id}}", IMAGES["NGINX_IMAGE"]).strip()
        srv.sh(f"docker tag {nginx_id} {foreign_tag}")
        img = _run("docker", "image", "inspect", "-f", "{{.Id}}", IMAGES["TEST_SERVER_IMAGE"]).strip()
        srv.sh(f"docker run -d --name searxng-fake --network none {img} sleep infinity >/dev/null")
        yield srv
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False,
                       timeout=120)  # fmt: skip


def _foreign(srv: Server) -> str:
    return srv.sh("docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "
                  "searxng-fake").stdout  # fmt: skip


def test_steps_full_cycle(server: Server) -> None:
    srv = server
    foreign = _foreign(srv)
    srv.ok("s1", "check")
    srv.ok("s0", "apply")
    srv.ok("s0", "check")
    for step in STEPS:
        before = srv.state(with_start=False)
        srv.ok(step, "apply")
        srv.ok(step, "check")
        first = srv.state()
        srv.ok(step, "apply")
        assert srv.state() == first, f"{step}: second apply changed the state"
        srv.ok(step, "rollback")
        assert srv.step(step, "check").returncode != 0, f"{step}: check passes after rollback"
        assert srv.state(with_start=False) == before, f"{step}: rollback left traces"
        srv.ok(step, "apply")

    # SEC-D4: nothing rendered for this server points at the real host (a 4th backend in the
    # template or a missed LLMHUB_RENDER_SET would show up here)
    rendered = srv.sh("cat /etc/llmhub/conf/config.yaml /etc/llmhub/nginx-weights.conf").stdout
    for addr in REAL_ADDRS:
        assert addr not in rendered, addr
    assert srv.sh(f"docker image inspect -f '{{{{.Id}}}}' {NGINX_FOREIGN_TAG}").stdout.strip()

    # weights through nginx + validator: a loading Mock instance, pinned TLS, Range, DB read
    # a prewarm lease (client e2e) launches a Mock instance: BOOTING for ~60 s
    r = srv.sh("docker exec -i llmhub-api python -c \"import sys, httpx; k = sys.stdin.readline()"
               ".strip(); r = httpx.post('http://127.0.0.1:8095/v1/leases', json={'model':"
               " 'qwen-27b-cloud', 'ttl_s': 600, 'prewarm': True}, headers={'Authorization':"
               " 'Bearer ' + k}); print(r.status_code)\" </etc/llmhub/keys/e2e.key")  # fmt: skip
    assert r.stdout.strip() == "201", r.stdout + r.stderr
    want = hashlib.sha256(MODEL[: 1024 * 1024]).hexdigest()
    r = srv.sh("for i in $(seq 30); do docker exec llmhub-weights-auth python -m"
               " llmhub.tools.weights_probe --config /etc/llmhub/conf/config.yaml --op auto"
               f" --rel qwen/model.gguf --expect-sha256 {want} --negatives && exit 0; sleep 1;"
               " done; exit 1", check=False)  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    secrets = srv.sh("cat /etc/llmhub/secrets/label /etc/llmhub/secrets/instance"
                     " /etc/llmhub/secrets/weights /etc/llmhub/admin.key").stdout  # fmt: skip
    assert all(s not in r.stdout + r.stderr for s in secrets.split())

    # Env of every container: paths only, never a secret value
    envs = srv.sh("for c in llmhub-api llmhub-reaper llmhub-weights-auth llmhub-weights-nginx;"
                  " do docker inspect -f '{{json .Config.Env}}' $c; done").stdout  # fmt: skip
    for s in secrets.split():
        assert s not in envs
    assert "LLMHUB_LABEL_SECRET_FILE=/run/llmhub-secrets/label" in envs

    # F7: the e2e client goes away at the end of S10. SEC-D2: the RUNNING API must refuse the
    # key (the config dir mount shows the new file; api/reaper are restarted on a change)
    srv.sh("install -m 0600 /etc/llmhub/keys/e2e.key /root/e2e.key.test")
    srv.ok("s10x", "apply")
    srv.ok("s10x", "check")
    probe = ("docker exec -i llmhub-api python -c \"import sys, httpx; k = sys.stdin.readline()"
             ".strip(); print(httpx.get('http://127.0.0.1:8095/v1/models', headers={"
             "'Authorization': 'Bearer ' + k}).status_code)\" </root/e2e.key.test")  # fmt: skip
    assert srv.sh(probe).stdout.strip() == "401"
    seen = srv.sh("docker exec llmhub-api cat /etc/llmhub/conf/config.yaml").stdout
    assert not [ln for ln in seen.splitlines() if ln.startswith("  e2e:")], seen
    srv.sh("shred -u /root/e2e.key.test")
    # nothing foreign changed (llama files, foreign container, foreign ufw rule)
    srv.ok("s11", "check")
    assert _foreign(srv) == foreign
    assert "ssh (foreign)" in srv.sh("cat /var/lib/ufw-stub/rules").stdout

    srv.ok("s14", "apply")
    srv.ok("s14", "check")
    left = srv.sh("docker ps -a --format '{{.Names}}'; getent passwd | grep -c llmhub || true;"
                  " ls /etc/llmhub /var/lib/llmhub 2>&1 || true; ls /root/llmhub-rollback-*.tgz;"
                  " stat -c %a /root/llmhub-rollback-*.tgz").stdout  # fmt: skip
    assert "llmhub-" not in left.split("\n")[0] and "searxng-fake" in left
    assert "No such file" in left and left.strip().endswith("600")
    assert _foreign(srv) == foreign
    assert srv.sh("cat /var/lib/ufw-stub/rules").stdout.strip() == "22/tcp # ssh (foreign)"
    # SEC-D5: the foreign tag of the shared nginx Id survived s3 rollback and s14
    tags = srv.sh("docker images --format '{{.Repository}}:{{.Tag}}'").stdout.split()
    assert NGINX_FOREIGN_TAG in tags and not [t for t in tags if t.startswith("llmhub")]
