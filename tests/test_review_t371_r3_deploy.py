"""T-371 review cycle 3 (re-review of fix round 1, commit 0e037b3).

Shell functions of `deploy/server/llmhub-deploy.sh` are extracted and run with stubs (no
docker, no root, no network). The R3-1..R3-4 tests were `xfail(strict=True)` until fix round 2;
the other tests pin behaviour the fix round relies on (restart/health-wait, record-based removal,
admin-only `teardown`, the socket guard, the backup temp file)."""

from __future__ import annotations

import importlib
import re
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

from api_fixtures import ADMIN_KEY, GAME_KEY, ApiEnv
from llmhub.clock import FakeClock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/server/llmhub-deploy.sh"
TEXT = SCRIPT.read_text()
backup_mod = importlib.import_module("llmhub.persistence.backup")


def _function(name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", TEXT, re.DOTALL | re.MULTILINE)
    assert m, name
    return m.group(0)


def _fns(*names: str) -> str:
    return "\n".join(_function(n) for n in names) + "\n"


def _bash(prog: str, tmp: Path) -> subprocess.CompletedProcess[str]:
    head = (
        "set -euo pipefail\n"
        "log() { printf '%s\\n' \"log: $*\" >&2; }\n"
        'die() { log "ERROR: $*"; exit 1; }\n'
        'fail() { log "check failed: $*"; return 1; }\n'
        f"MARK={tmp}/mark\nETC={tmp}/etc\nCONF={tmp}/etc/conf\nUID_API=64100\n"
        f"CALLS={tmp}/calls\n: >$CALLS\n"
        "UID_NGINX=64101\nLOGS=/var/log/llmhub\nMODELS=/opt/models\n"
    )
    return subprocess.run(["bash", "-c", head + prog], capture_output=True, text=True,
                          check=False, timeout=30)  # fmt: skip


def _calls(tmp: Path) -> list[str]:
    return (tmp / "calls").read_text().splitlines()


# ------------------------------------------------------------------ restart + health wait
def test_failed_health_wait_after_restart_stops_without_loop(tmp_path: Path) -> None:
    """A reader that never becomes healthy: each container is restarted exactly once, the wait
    is bounded by HEALTH_WAIT_S and the step fails (no restart loop, no stop/rm)."""
    prog = (
        "CONFIG_READERS=(llmhub-api llmhub-reaper llmhub-weights-auth)\nHEALTH_WAIT_S=6\n"
        "exists() { return 0; }\n"
        'dctl() { echo "dctl $*" >>$CALLS; }\n'
        "health() { echo starting; }\n"
        "sleep() { echo sleep >>$CALLS; }\n"
        + _fns("wait_healthy", "restart_config_readers")
        + "restart_config_readers\necho unreachable >>$CALLS\n"
    )
    r = _bash(prog, tmp_path)
    assert r.returncode == 1, r.stderr
    calls = _calls(tmp_path)
    assert [c for c in calls if c.startswith("dctl")] == [
        "dctl restart llmhub-api", "dctl restart llmhub-reaper", "dctl restart llmhub-weights-auth",
    ]  # fmt: skip
    assert calls.count("sleep") == 3  # 6 s budget / 2 s, shared by all containers
    assert "unreachable" not in calls
    assert "llmhub-api is starting" in r.stderr


def test_restart_readers_skips_missing_containers(tmp_path: Path) -> None:
    prog = (
        "CONFIG_READERS=(llmhub-api llmhub-reaper llmhub-weights-auth)\nHEALTH_WAIT_S=6\n"
        'exists() { [ "$1" = llmhub-api ]; }\n'
        'dctl() { echo "dctl $*" >>$CALLS; }\n'
        "health() { echo healthy; }\n"
        + _fns("wait_healthy", "restart_config_readers")
        + "restart_config_readers\n"
    )
    r = _bash(prog, tmp_path)
    assert r.returncode == 0, r.stderr
    assert _calls(tmp_path) == ["dctl restart llmhub-api"]


def _s5_prog(tmp: Path) -> str:
    (tmp / "etc" / "conf").mkdir(parents=True)
    (tmp / "etc" / "conf" / "config.yaml").write_text("old\n")
    return (
        "render_config() { printf 'bad\\n' >\"$CONF/config.yaml.new\"; }\n"
        "config_check() { echo config_check >>$CALLS; return 1; }\n"
        "restart_config_readers() { echo restart >>$CALLS; }\n"
        "chown() { :; }\nstat_is() { return 0; }\n"
        + _fns("install_checked_config", "install_config", "marker", "check_s5", "apply_s5")
        + "apply_s5\n"
    )


def test_s5_validates_new_config_before_restarting_readers(tmp_path: Path) -> None:
    """A config that fails `config-check` must not reach the running containers: today it is
    renamed into the mounted dir, api/reaper/validator are restarted on it (restart policy
    unless-stopped -> crash loop if `serve` rejects it) and only then checked."""
    r = _bash(_s5_prog(tmp_path), tmp_path)
    assert r.returncode != 0
    assert "restart" not in _calls(tmp_path)
    assert (tmp_path / "etc" / "conf" / "config.yaml").read_text() == "old\n"


# ------------------------------------------------------------------ images (SEC-D5)
def _s3_prog(tmp: Path, tag_ids: dict[str, str]) -> str:
    dist = tmp / "dist"
    dist.mkdir()
    (dist / "VERSION").write_text("0.2.1\n")
    (dist / "image.id").write_text("sha256:ours\n")
    (dist / "nginx.id").write_text("sha256:nginx\n")
    (dist / "image.config-id").write_text("sha256:ourcfg\n")
    (dist / "nginx.config-id").write_text("sha256:nginxcfg\n")
    cases = "".join(f"            {t}) echo {i} ;;\n" for t, i in tag_ids.items())
    return (
        f"DIST={dist}\nCONTAINERS=(llmhub-api llmhub-reaper llmhub-weights-auth llmhub-weights-nginx)\n"
        "docker() {\n"
        "    if [ \"$1 $2\" = 'image inspect' ]; then\n"
        "        case \"${@: -1}\" in\n" + cases + "            *) return 1 ;;\n        esac\n"
        "        return 0\n    fi\n"
        "    if [ \"$1\" = inspect ]; then return 1; fi\n"
        "    echo \"docker $*\" >>$CALLS\n}\n"
        + _fns("local_id_file", "dist_ids", "is_dist_id", "our_id", "image_id", "nginx_id",
               "release", "image_tag", "nginx_tag", "tag_id", "exists", "rollback_s3")
        + "rollback_s3\n"
    )  # fmt: skip


def test_rollback_s3_untags_our_tags_without_force(tmp_path: Path) -> None:
    prog = _s3_prog(tmp_path, {"llmhub:0.2.1": "sha256:ours", "llmhub-nginx:0.2.1": "sha256:nginx"})
    r = _bash(prog, tmp_path)
    assert r.returncode == 0, r.stderr
    assert _calls(tmp_path) == [
        "docker image rm llmhub:0.2.1",
        "docker image rm llmhub-nginx:0.2.1",
    ]


def test_rollback_s3_keeps_a_foreign_image_under_our_tag(tmp_path: Path) -> None:
    """`apply_s3` dies when `llmhub:<ver>` already names a foreign image, but `s3 rollback` /
    `s14` then remove that tag anyway (and with it the foreign image if it has no other tag)."""
    prog = _s3_prog(
        tmp_path, {"llmhub:0.2.1": "sha256:foreign", "llmhub-nginx:0.2.1": "sha256:nginx"}
    )
    r = _bash(prog, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "docker image rm llmhub:0.2.1" not in _calls(tmp_path)


def test_rollback_s3_untags_the_config_digest_of_a_graphdriver_store(tmp_path: Path) -> None:
    """R3-5: on a classic image store the tag Id is the config digest of dist; it is ours."""
    prog = _s3_prog(
        tmp_path, {"llmhub:0.2.1": "sha256:ourcfg", "llmhub-nginx:0.2.1": "sha256:nginxcfg"}
    )
    (tmp_path / "mark").mkdir()
    (tmp_path / "mark" / "image-0.2.1.local-id").write_text("sha256:ourcfg\n")
    r = _bash(prog, tmp_path)
    assert r.returncode == 0, r.stderr
    assert _calls(tmp_path) == ["docker image rm llmhub:0.2.1", "docker image rm llmhub-nginx:0.2.1"]
    assert not (tmp_path / "mark" / "image-0.2.1.local-id").exists()


def _id_prog(tmp: Path, local: str | None) -> str:
    dist = tmp / "dist"
    dist.mkdir()
    (dist / "VERSION").write_text("0.2.1\n")
    (dist / "image.id").write_text("sha256:index\n")
    (dist / "image.config-id").write_text("sha256:cfg\n")
    if local is not None:
        (tmp / "mark").mkdir()
        (tmp / "mark" / "image-0.2.1.local-id").write_text(local + "\n")
    return f"DIST={dist}\n" + _fns("release", "local_id_file", "dist_ids", "is_dist_id", "our_id",
                                   "image_id") + "image_id\n"  # fmt: skip


@pytest.mark.parametrize(
    ("local", "out", "rc"),
    [(None, "sha256:index", 0), ("sha256:cfg", "sha256:cfg", 0), ("sha256:index", "sha256:index", 0),
     ("sha256:evil", "", 1)],
)  # fmt: skip
def test_image_id_prefers_the_recorded_local_id(
    tmp_path: Path, local: str | None, out: str, rc: int
) -> None:
    r = _bash(_id_prog(tmp_path, local), tmp_path)
    assert (r.returncode, r.stdout) == (rc, out), r.stderr


def test_load_tagged_records_the_local_id_and_refuses_foreign(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "VERSION").write_text("0.2.1\n")
    (dist / "image.id").write_text("sha256:index\n")
    (dist / "image.config-id").write_text("sha256:cfg\n")
    fns = _fns("release", "local_id_file", "dist_ids", "is_dist_id", "load_tagged")
    head = f"DIST={dist}\ninstall() {{ mkdir -p \"${{@: -1}}\"; }}\n"
    ok = _bash(head + "tag_id() { echo sha256:cfg; }\n" + fns + "load_tagged image llmhub:0.2.1 x\n",
               tmp_path)  # fmt: skip
    assert ok.returncode == 0, ok.stderr
    assert (tmp_path / "mark" / "image-0.2.1.local-id").read_text() == "sha256:cfg\n"
    (tmp_path / "mark" / "image-0.2.1.local-id").unlink()
    bad = _bash(head + "tag_id() { echo sha256:foreign; }\n" + fns
                + "load_tagged image llmhub:0.2.1 x\n", tmp_path)  # fmt: skip
    assert bad.returncode != 0 and "foreign image under our tag" in bad.stderr
    assert not (tmp_path / "mark" / "image-0.2.1.local-id").exists()


def test_nginx_tarball_carries_only_our_tag() -> None:
    """`docker load` must never move a foreign tag (e.g. nginx:1.27-alpine) onto our image."""
    build = (ROOT / "deploy/docker/build.sh").read_text()
    assert 'docker save "$NGINX_TAG"' in build and 'docker save "$TAG"' in build
    assert 'docker save "$NGINX_IMAGE"' not in build


# ------------------------------------------------------------------ nginx conf (pre-existing)
def test_s8c_failed_dry_run_keeps_the_live_nginx_conf(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "nginx-weights.conf").write_text("old\n")
    prog = (
        f"STAGE={tmp_path}/mark/stage\nLOGS={tmp_path}/logs\nmkdir -p $LOGS\nAUTH_PORT=8096\nPUBLIC_IP=1.2.3.4\nWEIGHTS_PORT=9443\n"
        "RATE=80m\nMAX_DL=1\n"
        "install() {  # stub: drop -m/-o/-g, -d -> mkdir, else copy\n"
        "    local d=0 a=()\n"
        "    while [ $# -gt 0 ]; do case $1 in -m|-o|-g) shift 2 ;; -d) d=1; shift ;;"
        ' *) a+=("$1"); shift ;; esac; done\n'
        '    if [ $d = 1 ]; then mkdir -p "${a[@]}"; else cp "${a[0]}" "${a[1]}"; fi\n}\n'
        "image_id() { echo img; }\nnginx_id() { echo ngx; }\nexists() { return 1; }\n"
        "drun_once() { printf 'new\\n' >\"$STAGE/nginx-weights.conf\"; }\n"
        'docker() { echo "docker $*" >>$CALLS; return 1; }  # nginx -t fails\n'
        + _fns("stage_dir", "render_nginx_conf", "apply_s8c")
        + "apply_s8c\n"
    )
    r = _bash(prog, tmp_path)
    assert r.returncode != 0 and "nginx -t failed" in r.stderr
    assert (tmp_path / "etc" / "nginx-weights.conf").read_text() == "old\n"


def test_s8c_passed_dry_run_installs_the_checked_conf(tmp_path: Path) -> None:
    """R3-3: `nginx -t` runs on the .new file; only then it replaces the live conf."""
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "nginx-weights.conf").write_text("old\n")
    (tmp_path / "logrotate-llmhub-acct").write_text("same\n")
    prog = (
        f"STAGE={tmp_path}/mark/stage\nLOGS={tmp_path}/logs\nmkdir -p $LOGS\nAUTH_PORT=8096\n"
        f"MARK={tmp_path}/mark\nmkdir -p $MARK\n"
        f"PUBLIC_IP=1.2.3.4\nWEIGHTS_PORT=9443\nRATE=80m\nMAX_DL=1\nDIST={tmp_path}\n"
        f"LOGROTATE_FILE={tmp_path}/logrotate-llmhub-acct\n"
        "install() {\n"
        "    local d=0 a=()\n"
        "    while [ $# -gt 0 ]; do case $1 in -m|-o|-g) shift 2 ;; -d) d=1; shift ;;"
        ' *) a+=("$1"); shift ;; esac; done\n'
        '    if [ $d = 1 ]; then mkdir -p "${a[@]}"; else cp "${a[0]}" "${a[1]}"; fi\n}\n'
        "image_id() { echo img; }\nnginx_id() { echo ngx; }\nexists() { return 1; }\n"
        "drun_once() { printf 'new\\n    listen 1.2.3.4:9443 ssl;\\n' >\"$STAGE/nginx-weights.conf\"; }\n"
        # the stub copies the mounted dry-run conf so the test can inspect it
        'docker() { echo "docker $*" >>$CALLS; local a; for a in "$@"; do\n'
        '  case $a in *:/etc/nginx-weights/nginx.conf:ro) cp "${a%%:*}" "$MARK/dry.seen";; esac; done; }\n'
        'drun_named() { echo "run $1" >>$CALLS; }\n'
        "marker() { :; }\ncheck_s8c() { :; }\n"
        + _fns("stage_dir", "render_nginx_conf", "apply_s8c")
        + "apply_s8c\n"
    )
    r = _bash(prog, tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "etc" / "nginx-weights.conf").read_text() == "new\n    listen 1.2.3.4:9443 ssl;\n"
    assert not (tmp_path / "etc" / "nginx-weights.conf.new").exists()
    dry = next(c for c in _calls(tmp_path) if c.startswith("docker run"))
    assert f"{tmp_path}/mark/nginx-dry." in dry and ":/etc/nginx-weights/nginx.conf:ro" in dry
    # the dry run binds loopback (the public IP is absent under --network none); the copy is removed
    seen = (tmp_path / "mark" / "dry.seen").read_text()
    assert "listen 127.0.0.1:9443 ssl;" in seen and "1.2.3.4" not in seen
    assert not list((tmp_path / "mark").glob("nginx-dry.*"))


# ------------------------------------------------------------------ /opt/models readability
def test_s0_checks_the_sums_file_is_world_readable() -> None:
    """Renders run as 64100 (L6) and read /opt/models/sha256-T365.txt: preflight should report
    a missing o+r (file) / o+x (dir) before window 2 instead of a failing s8b."""
    body = _function("apply_s0")
    assert "$SUMS" in body or "$MODELS" in body


# ------------------------------------------------------------------ records (L1/L2) corrupt
def test_corrupt_account_record_removes_only_whitelisted_names(tmp_path: Path) -> None:
    (tmp_path / "mark").mkdir()
    (tmp_path / "mark" / "s2.created").write_text(
        "garbage\nuser llmhub extra\nuser root\nuser llmhub-x\ngroup ../etc\n"
        "user llmhub-wauth\n\ngroup llmhub-wauth\n"
    )
    prog = (
        'getent() { return 0; }\nuserdel() { echo "userdel $*" >>$CALLS; }\n'
        'groupdel() { echo "groupdel $*" >>$CALLS; }\n'
        + _fns("remove_recorded_accounts")
        + "remove_recorded_accounts\n"
    )
    r = _bash(prog, tmp_path)
    assert r.returncode == 0, r.stderr
    assert _calls(tmp_path) == ["userdel llmhub-wauth", "groupdel llmhub-wauth"]


def test_lost_ufw_record_keeps_rule_and_s14_check_reports_it() -> None:
    assert '[ -f "$MARK/ufw-$tag.added" ] && ufw_has' in _function("ufw_remove")
    assert 'ufw_has "llmhub api" || ufw_has "llmhub weights"' in _function("check_s14")


# ------------------------------------------------------------------ build.sh chmod
def test_world_readable_deploy_dir_holds_only_templates() -> None:
    build = (ROOT / "deploy/docker/build.sh").read_text()
    m = re.search(r'cp ((?:"\$ROOT/deploy/[^"]+"\s*\\?\s*)+)"\$STAGE/deploy/"', build)
    assert m, "deploy copy line"
    files = sorted(re.findall(r'"\$ROOT/deploy/([^"]+)"', m.group(1)))
    assert files == [
        "config.example.yaml",
        "gen_weights_cert.py",
        "nginx-weights.conf",
        "render.py",
    ]
    assert 'chmod -R u=rwX,go=rX "$STAGE/deploy"' in build
    for f in files:
        text = (ROOT / "deploy" / f).read_text()
        assert not re.search(r"lh_[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY", text), f


# ------------------------------------------------------------------ /admin/status teardown
async def test_teardown_view_is_admin_only(tmp_path: Path) -> None:
    env = ApiEnv(tmp_path)
    try:
        for key in (None, GAME_KEY, "lh_wrong_key_000000000000000"):
            r = await env.get("/admin/status", key=key)
            assert r.status_code in (401, 403), (key, r.status_code)
            assert "teardown" not in r.text
        r = await env.get("/admin/status", key=ADMIN_KEY)
        assert r.status_code == 200 and "teardown" in r.json()
    finally:
        await env.aclose()


# ------------------------------------------------------------------ socket guard
def test_guard_lets_localhost_through() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        for host in ("127.0.0.1", "localhost"):
            with socket.create_connection((host, srv.getsockname()[1]), timeout=2):
                pass
            srv.accept()[0].close()
    finally:
        srv.close()


# ------------------------------------------------------------------ backup temp file
def test_failed_backup_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.execute("CREATE TABLE t(x)")
    dst = tmp_path / "backup"

    def boom(_out: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(backup_mod, "_to_rollback_journal", boom)
    with pytest.raises(sqlite3.OperationalError):
        backup_mod.backup(conn, dst, FakeClock(), prefix=backup_mod.CONTROLLER_PREFIX)
    conn.close()
    assert list(dst.iterdir()) == []


def test_backup_temp_names_never_match_rotation_globs(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.execute("CREATE TABLE t(x)")
    out = backup_mod.backup(conn, tmp_path / "b", FakeClock(), prefix=backup_mod.CONTROLLER_PREFIX)
    conn.close()
    assert out.name.startswith("controller-") and out.suffix == ".db"
    assert oct(out.stat().st_mode & 0o777) == "0o640"
    assert [p.name for p in (tmp_path / "b").iterdir()] == [out.name]


# ------------------------------------------------------------------ dist == source
def test_dist_release_matches_sources() -> None:
    ver = re.search(r'__version__ = "(.*)"', (ROOT / "src/llmhub/__init__.py").read_text())
    assert ver
    dist = ROOT / "dist" / ver.group(1)
    if not dist.is_dir():
        pytest.skip("no local dist build")
    pairs = {
        "llmhub-deploy.sh": "deploy/server/llmhub-deploy.sh",
        "logrotate-llmhub-acct": "deploy/logrotate-llmhub-acct",
        "e2e_mock.py": "scripts/e2e_mock.py",
    }
    for d, s in pairs.items():
        assert (dist / d).read_bytes() == (ROOT / s).read_bytes(), d
    for sub, src in (("run-args", "deploy/docker/run-args"), ("systemd", "deploy/systemd")):
        for f in (ROOT / src).iterdir():
            assert (dist / sub / f.name).read_bytes() == f.read_bytes(), f
    r = subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS"], cwd=dist,
                       capture_output=True, text=True, check=False, timeout=120)  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
