"""T-371 B: operator CLI (`python -m llmhub ...`) - key files, keys new --out, admin commands
against a live stack, config-check, serve exit 75, --no-docs, backup, healthcheck."""

from __future__ import annotations

import io
import json
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from api_fixtures import ADMIN_KEY, GAME_KEY
from int_live import LiveStack
from llmhub import __main__ as cli
from llmhub.admin_cli import AdminCliError, base_url, read_key_file, write_kill_file
from llmhub.api_gateway.auth import hash_key
from llmhub.configuration.loader import load_config

ROOT = Path(__file__).resolve().parent.parent


def _keyfile(tmp: Path, text: str = ADMIN_KEY, mode: int = 0o600, name: str = "admin.key") -> Path:
    p = tmp / name
    p.write_text(text + "\n")
    p.chmod(mode)
    return p


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


# ---------------------------------------------------------------- B1: key files, keys new


def test_read_key_file_ok(tmp_path: Path) -> None:
    assert read_key_file(_keyfile(tmp_path)) == ADMIN_KEY


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_read_key_file_refuses_group_or_other(tmp_path: Path, mode: int) -> None:
    with pytest.raises(AdminCliError, match="0600") as ei:
        read_key_file(_keyfile(tmp_path, mode=mode))
    assert ADMIN_KEY not in str(ei.value)


def test_read_key_file_refuses_symlink(tmp_path: Path) -> None:
    real = _keyfile(tmp_path)
    link = tmp_path / "link.key"
    link.symlink_to(real)
    with pytest.raises(AdminCliError):
        read_key_file(link)


def test_read_key_file_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(ADMIN_KEY + "\n"))
    assert read_key_file(Path("/dev/stdin")) == ADMIN_KEY
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(AdminCliError):
        read_key_file(Path("/dev/stdin"))


def test_keys_new_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "voicebot.key"
    code, stdout, stderr = _run(["keys", "new", "--name", "voicebot", "--out", str(out)], capsys)
    assert code == 0
    key = out.read_text().strip()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert key and key not in stdout and key not in stderr
    assert hash_key(key) in stdout and "clients.voicebot.key_sha256" in stdout
    # an existing file is never overwritten
    code, _, err = _run(["keys", "new", "--out", str(out)], capsys)
    assert code == 2 and "exists" in err
    assert out.read_text().strip() == key


def test_keys_new_without_out_still_prints_once(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _run(["keys", "new"], capsys)
    assert code == 0 and out.startswith("key: ")


def test_base_url(tmp_path: Path) -> None:
    cfg = load_config(_config(tmp_path))
    assert base_url(cfg, None) == "http://127.0.0.1:8095"
    assert base_url(cfg, "http://x:1/") == "http://x:1"
    cfg.api.host = "::1"
    assert base_url(cfg, None) == "http://[::1]:8095"


def test_write_kill_file(tmp_path: Path) -> None:
    p = write_kill_file(tmp_path)
    assert p == tmp_path / "KILL" and p.exists()


# ---------------------------------------------------------------- B2: against a live stack


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveStack]:
    stack = LiveStack(tmp_path_factory.mktemp("cli-live")).start()
    yield stack
    stack.stop()


@pytest.fixture
def admin(tmp_path: Path, live: LiveStack) -> list[str]:
    return ["--url", live.url, "--admin-key-file", str(_keyfile(tmp_path))]


def _json(out: str) -> dict[str, Any]:
    return json.loads(out)


def test_status_budget_clients(
    live: LiveStack, admin: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(["status", *admin], capsys)
    assert code == 0 and _json(out)["halted"] is False
    code, out, _ = _run(["budget", *admin], capsys)
    assert code == 0 and "month" in _json(out)
    code, out, _ = _run(["client", "list", *admin], capsys)
    assert code == 0 and {c["name"] for c in _json(out)["clients"]} >= {"game"}
    assert ADMIN_KEY not in out


def test_hold_release(live: LiveStack, admin: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(["hold", "--pool", "p1", "--ttl", "60", *admin], capsys)
    assert code == 0, err
    assert _json(out)["lease_id"]
    code, out, _ = _run(["release", "--pool", "p1", *admin], capsys)
    assert code == 0 and _json(out)["released"] is True
    code, _, err = _run(["hold", "--pool", "nope", "--ttl", "60", *admin], capsys)
    assert code == 1 and "404" in err


def test_clear_weights_hold(
    live: LiveStack, admin: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    from llmhub.cloud_manager.provisioner import set_weights_hold

    assert live.svc is not None
    conn, cfg = live.svc.rt.conn, live.svc.rt.cfg
    live.call(lambda: set_weights_hold(conn, "p1", time.time(), cfg, "test"))
    code, out, _ = _run(["clear-weights-hold", "--pool", "p1", *admin], capsys)
    assert code == 0 and _json(out)["removed"] is True
    code, out, _ = _run(["clear-weights-hold", "--pool", "p1", *admin], capsys)
    assert code == 0 and _json(out)["removed"] is False


def test_kill_resume(live: LiveStack, admin: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(["kill", "--reason", "cli-test", *admin], capsys)
    assert code == 0, err
    code, out, _ = _run(["status", *admin], capsys)
    assert _json(out)["halted"] is True
    code, out, err = _run(["resume", *admin], capsys)
    assert code == 0, err
    assert _json(out)["halted"] is False


def test_kill_file_blocks_resume(
    live: LiveStack, tmp_path: Path, admin: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    cfgp = tmp_path / "live.yaml"
    cfgp.write_text(yaml.safe_dump({"data_dir": str(live.cfg.data_dir), "pools": {}}))
    code, out, _ = _run(["kill", "--file", "--config", str(cfgp)], capsys)
    assert code == 0
    kill = Path(_json(out)["kill_file"])
    try:
        assert _status(admin, capsys)["kill_file"] is True
        code, _, err = _run(["resume", *admin], capsys)
        assert code == 1 and "kill_file_present" in err
    finally:
        kill.unlink()
    live.wait_for(lambda: _run(["resume", *admin], capsys)[0] == 0, timeout=30)


def _status(admin: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return _json(_run(["status", *admin], capsys)[1])


def test_reload_clients(live: LiveStack, admin: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _run(["reload-clients", *admin], capsys)
    # LiveStack has no config file: the API refuses with 400/409, the CLI reports it as 1
    assert code in (0, 1)
    if code == 1:
        assert "HTTP 4" in err


def test_wrong_key_is_1_without_leak(
    live: LiveStack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _keyfile(tmp_path, GAME_KEY, name="client.key")
    code, out, err = _run(["status", "--url", live.url, "--admin-key-file", str(bad)], capsys)
    assert code == 1 and "403" in err
    assert GAME_KEY not in out + err
    wrong = _keyfile(tmp_path, "lh_definitely_not_the_admin_key", name="wrong.key")
    code, out, err = _run(["status", "--url", live.url, "--admin-key-file", str(wrong)], capsys)
    assert code == 1 and "401" in err and "lh_definitely" not in out + err


def test_unreachable_api_is_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _run(["status", "--url", "http://127.0.0.1:9", "--admin-key-file",
                         str(_keyfile(tmp_path)), "--timeout", "2"], capsys)  # fmt: skip
    assert code == 1 and "ConnectError" in err


def test_bad_key_file_is_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = _run(["status", "--url", "http://127.0.0.1:9", "--admin-key-file",
                       str(_keyfile(tmp_path, mode=0o644))], capsys)  # fmt: skip
    assert code == 2


# ---------------------------------------------------------------- B3: local commands


def _config(tmp: Path, **over: Any) -> Path:
    raw: dict[str, Any] = {"data_dir": str(tmp / "data"), "pools": {}}
    raw.update(over)
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def _secret(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text)
    p.chmod(0o640)
    return p


def test_config_check_ok_with_file_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for k in list(os.environ):
        if k.startswith("LLMHUB_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("LLMHUB_LABEL_SECRET_FILE", str(_secret(tmp_path, "l", "l" * 48)))
    monkeypatch.setenv("LLMHUB_INSTANCE_SECRET_FILE", str(_secret(tmp_path, "i", "i" * 48)))
    monkeypatch.setenv("LLMHUB_WEIGHTS_SECRET_FILE", str(_secret(tmp_path, "w", "w" * 48)))
    code, out, _ = _run(["config-check", "--config", str(_config(tmp_path))], capsys)
    assert (code, out.strip()) == (0, "ok")
    # same instance and weights secret -> refused, no secret in the output
    monkeypatch.setenv("LLMHUB_WEIGHTS_SECRET_FILE", str(_secret(tmp_path, "w2", "i" * 48)))
    code, out, err = _run(["config-check", "--config", str(_config(tmp_path))], capsys)
    assert code == 2 and "must differ" in err and "i" * 48 not in out + err


def test_config_check_mock_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLMHUB_LABEL_SECRET", "l" * 48)
    e2e = {"key_sha256": "b" * 64, "allowed_models": [], "cloud_allowed": True, "mock_only": True}
    p = _config(tmp_path, clients={"e2e": e2e}, providers={"enabled": ["mock", "runpod"]})
    code, _, err = _run(["config-check", "--config", str(p)], capsys)
    assert code == 2 and "mock_only" in err


def test_config_check_missing_label_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for k in list(os.environ):
        if k.startswith("LLMHUB_"):
            monkeypatch.delenv(k)
    code, _, err = _run(["config-check", "--config", str(_config(tmp_path))], capsys)
    assert code == 2 and "LLMHUB_LABEL_SECRET" in err


def test_serve_state_db_busy_exits_75(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from llmhub import server
    from llmhub.persistence.integrity import StateDbBusy

    def busy(*_a: Any, **_k: Any) -> Any:
        raise StateDbBusy("state.db is locked")

    monkeypatch.setattr(server, "build_service", busy)
    monkeypatch.setenv("LLMHUB_LABEL_SECRET", "l" * 48)
    old = os.umask(0o022)
    try:
        code, _, err = _run(["serve", "--config", str(_config(tmp_path))], capsys)
        applied = os.umask(0o022)
    finally:
        os.umask(old)
    assert code == 75 and "busy" in err
    assert applied == 0o027  # umask set before the DB is opened


def test_serve_no_docs_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from llmhub import server

    seen: list[bool] = []

    def fake_build(cfg: Any, *_a: Any, **_k: Any) -> Any:
        seen.append(cfg.api.docs)
        raise server.ConfigError("stop here")

    monkeypatch.setattr(server, "build_service", fake_build)
    old = os.umask(0o022)
    try:
        assert _run(["serve", "--config", str(_config(tmp_path)), "--no-docs"], capsys)[0] == 2
        assert _run(["serve", "--config", str(_config(tmp_path))], capsys)[0] == 2
    finally:
        os.umask(old)
    assert seen == [False, True]


def test_backup_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from llmhub.persistence.db import connect, migrate

    cfgp = _config(tmp_path)
    (tmp_path / "data").mkdir()
    conn = connect(tmp_path / "data" / "state.db")
    migrate(conn)
    conn.close()
    old = os.umask(0o022)
    try:
        code, out, err = _run(["backup", "--config", str(cfgp), "--to", str(tmp_path / "bk"),
                               "--keep", "3"], capsys)  # fmt: skip
    finally:
        os.umask(old)
    assert code == 0, err
    made = Path(_json(out)["backup"])
    assert made.exists() and stat.S_IMODE(made.stat().st_mode) == 0o640
    code, _, err = _run(["backup", "--config", str(_config(tmp_path, data_dir=str(tmp_path / "x"))),
                         "--to", str(tmp_path / "bk")], capsys)  # fmt: skip
    assert code == 1 and "FileNotFoundError" in err


def test_healthcheck_command(live: LiveStack, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(["healthcheck", "--url", live.url + "/healthz"], capsys)[0] == 0
    assert _run(["healthcheck", "--url", live.url + "/metrics"], capsys)[0] == 1  # 401
    assert _run(["healthcheck", "--url", "http://127.0.0.1:9/x", "--timeout", "1"], capsys)[0] == 1


def test_cli_module_does_not_import_controller() -> None:
    src = (ROOT / "src/llmhub/admin_cli.py").read_text()
    assert "llmhub.controller" not in src and "llmhub.server" not in src
