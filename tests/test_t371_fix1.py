"""T-371 fix round 1: regression tests for the review findings that the review files do not
cover (L1-L6, I1, REV2-3 status view, REV2-5, REV2-7, the SEC-D4 socket guard).

Shell functions of `deploy/server/llmhub-deploy.sh` are extracted and run with stubs (no
docker, no root, no network)."""

from __future__ import annotations

import re
import socket
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api_fixtures import ADMIN_KEY, ApiEnv
from conftest import BLOCKED_ATTEMPTS, blocked_address
from llmhub.clock import FakeClock
from llmhub.persistence.backup import (
    BACKUP_DIR,
    CONTROLLER_PREFIX,
    backup,
    backup_state_db,
)
from llmhub.persistence.integrity import open_state_db

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/server/llmhub-deploy.sh"
TEXT = SCRIPT.read_text()
CODE = "\n".join(ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("#"))


def _function(name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", TEXT, re.DOTALL | re.MULTILINE)
    assert m, name
    return m.group(0)


def _bash(prog: str, tmp: Path) -> subprocess.CompletedProcess[str]:
    head = (
        "set -euo pipefail\n"
        "log() { printf '%s\\n' \"log: $*\" >&2; }\n"
        "die() { log \"ERROR: $*\"; exit 1; }\n"
        "fail() { log \"check failed: $*\"; return 1; }\n"
        f"MARK={tmp}/mark\n"
    )
    return subprocess.run(["bash", "-c", head + prog], capture_output=True, text=True,
                          check=False, timeout=30)  # fmt: skip


# ------------------------------------------------------------------ SEC-D4 socket guard
@pytest.mark.parametrize(
    ("addr", "blocked"),
    [
        (("10.8.0.1", 9), True), (("10.8.0.200", 8095), True), (("65.109.95.35", 9443), True),
        (("::ffff:10.8.0.1", 8091, 0, 0), True), (("127.0.0.1", 8095), False),
        (("10.8.1.1", 80), False), (("localhost", 80), False), ("/tmp/sock", False),
    ],
)  # fmt: skip
def test_guard_classifies_addresses(addr: object, blocked: bool) -> None:
    assert blocked_address(addr) is blocked


def test_guard_blocks_real_connect() -> None:
    with socket.socket() as s, pytest.raises(PermissionError, match="network guard"):
        s.connect(("10.8.0.1", 9))
    with socket.socket() as s, pytest.raises(PermissionError):
        s.connect_ex(("65.109.95.35", 9443))


async def test_guard_blocks_httpx() -> None:
    import httpx

    before = len(BLOCKED_ATTEMPTS)
    async with httpx.AsyncClient() as c:
        with pytest.raises((PermissionError, httpx.ConnectError)):
            await c.get("http://10.8.0.1:9/slots", timeout=2)
    assert any("10.8.0.1" in a for a in BLOCKED_ATTEMPTS[before:])


# ------------------------------------------------------------------ L1 ufw
UFW_STUB = r"""
UFW=ufw_stub
ufw_stub() {
  case "$1" in
    status) cat "$RULES" ;;
    allow) shift; spec=(); c=""
           while [ $# -gt 0 ]; do if [ "$1" = comment ]; then c=$2; shift 2; else spec+=("$1"); shift; fi; done
           echo "${spec[*]} # $c" >>"$RULES" ;;
    delete) shift 2; { grep -vF "$* #" "$RULES" || true; } >"$RULES.t"; mv "$RULES.t" "$RULES" ;;
  esac
}
"""


def _ufw_prog(tmp: Path, rules: str, body: str) -> str:
    (tmp / "rules").write_text(rules)
    return (
        f"RULES={tmp}/rules\n" + UFW_STUB
        + "\n".join(_function(f) for f in ("ufw_has", "ufw_foreign", "ufw_add", "ufw_remove"))
        + "\n" + body
    )  # fmt: skip


def test_ufw_add_records_and_remove_deletes_only_ours(tmp_path: Path) -> None:
    prog = _ufw_prog(tmp_path, "22/tcp # ssh\n",
                     "ufw_add api 'llmhub api' 8095 in to 1.2.3.4 port 8095 proto tcp\n"
                     "test -f $MARK/ufw-api.added\n"
                     "ufw_remove api 'llmhub api' in to 1.2.3.4 port 8095 proto tcp\n")  # fmt: skip
    r = _bash(prog, tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "rules").read_text() == "22/tcp # ssh\n"
    assert not (tmp_path / "mark" / "ufw-api.added").exists()


def test_ufw_add_refuses_a_foreign_rule_on_the_port(tmp_path: Path) -> None:
    prog = _ufw_prog(tmp_path, "8095/tcp ALLOW Anywhere # someone else\n",
                     "ufw_add api 'llmhub api' 8095 in port 8095 proto tcp\n")  # fmt: skip
    r = _bash(prog, tmp_path)
    assert r.returncode != 0 and "foreign rule for port 8095" in r.stderr
    assert "llmhub" not in (tmp_path / "rules").read_text()


def test_ufw_remove_keeps_unrecorded_rule(tmp_path: Path) -> None:
    rules = "in port 8095 proto tcp # llmhub api\n"
    r = _bash(_ufw_prog(tmp_path, rules, "ufw_remove api 'llmhub api' in port 8095 proto tcp\n"),
              tmp_path)  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "rules").read_text() == rules and "not added by this script" in r.stderr


# ------------------------------------------------------------------ L2 accounts
def test_s14_removes_only_recorded_accounts(tmp_path: Path) -> None:
    (tmp_path / "mark").mkdir()
    (tmp_path / "mark" / "s2.created").write_text("group llmhub\nuser llmhub\nuser root\n")
    log = tmp_path / "calls"
    prog = (
        f"getent() {{ return 0; }}\nuserdel() {{ echo \"userdel $*\" >>{log}; }}\n"
        f"groupdel() {{ echo \"groupdel $*\" >>{log}; }}\n"
        + _function("remove_recorded_accounts") + "\nremove_recorded_accounts\n"
    )
    r = _bash(prog, tmp_path)
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == ["userdel llmhub", "groupdel llmhub"]
    assert "llmhub-wauth" not in log.read_text()
    assert "remove_recorded_accounts" in _function("apply_s14")
    assert "userdel" not in _function("apply_s14")


def test_no_record_keeps_accounts(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    prog = (f"userdel() {{ echo x >>{log}; }}\ngroupdel() {{ echo x >>{log}; }}\n"
            + _function("remove_recorded_accounts") + "\nremove_recorded_accounts\n")  # fmt: skip
    r = _bash(prog, tmp_path)
    assert r.returncode == 0 and not log.exists()


# ------------------------------------------------------------------ L3 / L4 listeners
def _listeners(tmp: Path, base: str, now: str) -> subprocess.CompletedProcess[str]:
    (tmp / "base").write_text(base)
    (tmp / "now").write_text(now)
    prog = (
        "API_HOST=10.8.0.1 API_PORT=8095 AUTH_PORT=8096 PUBLIC_IP=65.109.95.35 WEIGHTS_PORT=9443\n"
        + _function("is_wildcard") + "\n" + _function("new_listeners")
        + f"\nnew_listeners {tmp}/base {tmp}/now\n"
    )
    return _bash(prog, tmp)


def test_s11_accepts_exactly_our_listeners(tmp_path: Path) -> None:
    r = _listeners(tmp_path, "10.8.0.1:22\n",
                   "10.8.0.1:22\n10.8.0.1:8095\n127.0.0.1:8096\n65.109.95.35:9443\n"
                   "127.0.0.1:5432\n")  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "informational): 127.0.0.1:5432" in r.stderr


@pytest.mark.parametrize("bad", ["0.0.0.0:8095", "[::]:9443", "*:7000", "0.0.0.0:22",
                                 "65.109.95.35:8095", "10.8.0.1:9443", "0.0.0.0:8096"])  # fmt: skip
def test_s11_fails_on_wildcard_or_misplaced_listener(tmp_path: Path, bad: str) -> None:
    r = _listeners(tmp_path, "", f"{bad}\n")
    assert r.returncode != 0, r.stderr


def test_s0_checks_wildcards_and_foreign_rules() -> None:
    body = _function("apply_s0")
    assert 'no_wildcard "$p"' in body and 'ufw_foreign "$p"' in body
    assert "new_listeners" in _function("check_s11")


# ------------------------------------------------------------------ L5 / L6 / I1 / SEC-D2
def test_nginx_dry_run_is_hardened_and_never_writes_logs() -> None:
    body = _function("apply_s8c")
    run = body[body.index("docker run --rm"): body.index('-t -q -e stderr')]
    assert "no-new-privileges:true" in run and "--pids-limit" in run
    assert '--tmpfs "$LOGS:' in run and '-v "$LOGS:$LOGS"' not in run


@pytest.mark.parametrize("fn", ["render_config", "render_nginx_conf", "new_key"])
def test_renders_run_unprivileged_in_a_private_stage(fn: str) -> None:
    body = _function(fn)
    assert "stage_dir" in body and '-v "$STAGE:/out"' in body
    assert "--user 0" not in body and '-v "$ETC:/out"' not in body and "$ETC/keys:/out" not in body
    assert '--user "$UID_API:$UID_API"' in body


def test_stage_dir_is_private() -> None:
    body = _function("stage_dir")
    assert 'install -d -m 0700 -o "$UID_API" -g "$UID_API" "$STAGE"' in body
    assert "STAGE=$MARK/stage" in CODE


def test_daemon_reload_refuses_pending_llama_edits(tmp_path: Path) -> None:
    prog = (
        "SYSTEMCTL=sysstub\n"
        "sysstub() { if [ \"$1\" = show ]; then echo NeedDaemonReload=$PENDING; else echo RELOADED; fi; }\n"
        + _function("daemon_reload")
    )  # fmt: skip
    ok = _bash("PENDING=no\n" + prog + "\ndaemon_reload\n", tmp_path)
    assert ok.returncode == 0 and "RELOADED" in ok.stdout
    bad = _bash("PENDING=yes\n" + prog + "\ndaemon_reload\n", tmp_path)
    assert bad.returncode != 0 and "RELOADED" not in bad.stdout
    assert '"$SYSTEMCTL" daemon-reload' not in CODE.replace(_function("daemon_reload"), "")


@pytest.mark.parametrize("fn", ["apply_s5", "apply_s8b", "rollback_s8b", "apply_s10x", "rollback_s5"])
def test_config_change_restarts_readers(fn: str) -> None:
    assert "restart_config_readers" in _function(fn)


def test_restart_readers_covers_api_reaper_validator() -> None:
    assert "CONFIG_READERS=(llmhub-api llmhub-reaper llmhub-weights-auth)" in CODE
    body = _function("restart_config_readers")
    assert 'dctl restart "$c"' in body and "wait_healthy" in body


def test_s10x_check_asks_the_running_api() -> None:
    check = _function("check_s10x")
    assert "admin_call client list" in check
    apply = _function("apply_s10x")
    assert apply.index("key_status") < apply.index("shred -u")
    assert "die" in apply


def test_backup_unit_and_cli_use_the_config_dir() -> None:
    svc = (ROOT / "deploy/systemd/llmhub-backup.service").read_text()
    assert "--config /etc/llmhub/conf/config.yaml --to /var/lib/llmhub/backup" in svc
    from llmhub.admin_cli import DEFAULT_CONFIG

    assert DEFAULT_CONFIG == "/etc/llmhub/conf/config.yaml"


# ------------------------------------------------------------------ REV2-3 status view
async def test_admin_status_counts_teardown_rows(tmp_path: Path) -> None:
    env = ApiEnv(tmp_path)
    try:
        r = await env.get("/admin/status", key=ADMIN_KEY)
        assert r.json()["teardown"] == {}
        for i, st in enumerate(("PENDING_DELETE", "LEAKED", "LEAKED", "STOPPED")):
            env.h.conn.execute(
                "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
                " created_at, max_lifetime_at) VALUES (?, ?, 'p1', 'mock', ?, ?, 0, 0, 1e12)",
                (f"i{i}", f"{i:012x}", f"l{i}", st),
            )
        r = await env.get("/admin/status", key=ADMIN_KEY)
        assert r.json()["teardown"] == {"PENDING_DELETE": 1, "LEAKED": 2}
        assert r.json()["pools"]["p1"]["instance"] is None  # hidden from the pool view
    finally:
        await env.aclose()


# ------------------------------------------------------------------ REV2-5 / REV2-7 backups
def _journal_mode(path: Path) -> str:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(c.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        c.close()


def test_backup_copies_are_not_wal(tmp_path: Path) -> None:
    r = open_state_db(tmp_path / "state.db", FakeClock())
    try:
        assert r.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        out = backup_state_db(tmp_path / "state.db", tmp_path / "bk", keep=3,
                              now=datetime(2026, 11, 4, tzinfo=UTC))  # fmt: skip
        auto = backup(r.conn, tmp_path / "bk", FakeClock(), prefix=CONTROLLER_PREFIX)
    finally:
        r.conn.close()
    for p in (out, auto):
        assert _journal_mode(p) == "delete"
        sqlite3.connect(str(p)).execute("SELECT count(*) FROM sqlite_master").fetchone()
        assert not Path(f"{p}-wal").exists()


def test_controller_and_timer_rotations_are_independent(tmp_path: Path) -> None:
    assert BACKUP_DIR == "backup"
    r = open_state_db(tmp_path / "state.db", FakeClock())
    bk = tmp_path / BACKUP_DIR
    try:
        for k in range(3):
            backup_state_db(tmp_path / "state.db", bk, keep=14,
                            now=datetime(2026, 11, 4, 0, 0, k, tzinfo=UTC))  # fmt: skip
        clock = FakeClock()
        for _ in range(9):
            backup(r.conn, bk, clock, keep=7, prefix=CONTROLLER_PREFIX)
            clock.advance_to(clock.now() + 1)
    finally:
        r.conn.close()
    assert len(list(bk.glob("state-*.db"))) == 3
    assert len(list(bk.glob(f"{CONTROLLER_PREFIX}*.db"))) == 7
