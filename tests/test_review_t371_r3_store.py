"""T-371 review cycle 3, R3-5: image Ids on a dockerd with the classic (graphdriver) image store.

`dist/<ver>/image.id` and `nginx.id` are OCI *index* digests (the workstation runs the containerd
image store). A dockerd with a graphdriver store (overlay2/vfs: every Docker upgraded from < 29
with existing data keeps it) reports the *config* digest as `.Id` after `docker load`, and
`docker run <index digest>` fails. `apply_s3` would die on such a server and no container
could start by Id. The test starts a disposable dockerd (`llmhub-test-rev3-store-*`,
`--network none`, `--storage-driver vfs`), shares only its socket with the host user and runs
the real `apply_s3` functions against it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/server/llmhub-deploy.sh"
IMAGES = dict(
    line.split("=", 1)
    for line in (ROOT / "deploy/docker/images.env").read_text().splitlines()
    if line and not line.startswith("#")
)
VER_M = re.search(r'__version__ = "(.*)"', (ROOT / "src/llmhub/__init__.py").read_text())
DIST = ROOT / "dist" / (VER_M.group(1) if VER_M else "missing")


FUNCS = ("drun_once", "local_id_file", "dist_ids", "is_dist_id", "our_id", "image_id",
         "nginx_id", "release", "image_tag", "nginx_tag", "tag_id", "check_s3", "load_tagged",
         "apply_s3")  # fmt: skip


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.DOTALL | re.MULTILINE)
    assert m, name
    return m.group(0)


def _image_ok(ref: str) -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", ref], check=False, capture_output=True)
    return r.returncode == 0


@pytest.mark.docker
@pytest.mark.skipif(not _image_ok(IMAGES["TEST_DIND_IMAGE"]), reason="dind image not local")
@pytest.mark.skipif(not (DIST / "image.id").is_file(), reason="no local dist build")
def test_s3_works_on_a_graphdriver_image_store(tmp_path: Path) -> None:
    sock = tmp_path / "sock"
    sock.mkdir()
    name = f"llmhub-test-rev3-store-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--privileged", "--name", name, "--network", "none",
         "-v", f"{sock}:/run/dsock", IMAGES["TEST_DIND_IMAGE"], "dockerd",
         "--host=unix:///run/dsock/docker.sock", f"--group={os.getgid()}",
         "--iptables=false", "--bridge=none", "--storage-driver=vfs"],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    try:
        env = {**os.environ, "DOCKER_HOST": f"unix://{sock}/docker.sock"}
        for _ in range(60):
            if subprocess.run(["docker", "info"], env=env, capture_output=True,
                              check=False).returncode == 0:  # fmt: skip
                break
            time.sleep(1)
        prog = (
            "set -euo pipefail\n"
            "log() { printf '%s\\n' \"log: $*\" >&2; }\n"
            'die() { log "ERROR: $*"; exit 1; }\n'
            'fail() { log "check failed: $*"; return 1; }\n'
            f"DIST={DIST}\nUID_API=64100\nMARK={tmp_path}/mark\n"
            + "\n".join(_function(f) for f in FUNCS)
            + "\napply_s3\n"
        )
        r = subprocess.run(["bash", "-c", prog], env=env, capture_output=True, text=True,
                           check=False, timeout=600)  # fmt: skip
        assert r.returncode == 0, r.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False,
                       timeout=120)  # fmt: skip
