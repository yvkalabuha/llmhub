"""T-373 A2: client key from env or a private file; never printed."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from llmhub.mcp_server import secret as secret_mod
from llmhub.mcp_server.secret import KeyUnavailable, Secret, load_key

KEY = "lh_test_secret_value_123456"


def _keyfile(tmp: Path, text: str = KEY + "\n", mode: int = 0o600) -> Path:
    p = tmp / "key"
    p.write_text(text)
    p.chmod(mode)
    return p


def test_env_wins_over_file(tmp_path: Path) -> None:
    assert load_key("from-env", _keyfile(tmp_path)).reveal() == "from-env"


def test_file_ok_and_first_nonempty_line(tmp_path: Path) -> None:
    assert load_key(None, _keyfile(tmp_path, f"\n  {KEY}  \nsecond\n")).reveal() == KEY


def _reason(env: str | None, path: Path) -> str:
    with pytest.raises(KeyUnavailable) as exc:
        load_key(env, path)
    assert KEY not in str(exc.value) and KEY not in repr(exc.value)
    assert str(path) not in str(exc.value)
    return exc.value.reason


def test_missing(tmp_path: Path) -> None:
    assert _reason(None, tmp_path / "nope") == "missing"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_bad_mode(tmp_path: Path, mode: int) -> None:
    assert _reason(None, _keyfile(tmp_path, mode=mode)) == "bad_mode"


def test_symlink_not_regular(tmp_path: Path) -> None:
    target = _keyfile(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target)
    assert _reason(None, link) == "not_regular"


def test_directory_not_regular(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir(mode=0o700)
    assert _reason(None, d) == "not_regular"


def test_bad_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _keyfile(tmp_path)
    real = os.fstat

    def fake_fstat(fd: int) -> Any:
        st = real(fd)
        fields = list(st)
        fields[4] = os.getuid() + 1  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(secret_mod.os, "fstat", fake_fstat)
    assert _reason(None, path) == "bad_owner"


@pytest.mark.parametrize("text", ["", "\n  \n"])
def test_empty(tmp_path: Path, text: str) -> None:
    assert _reason(None, _keyfile(tmp_path, text)) == "empty"


def test_secret_never_printed() -> None:
    s = Secret(KEY)
    assert KEY not in repr(s) and KEY not in str(s) and KEY not in f"{s}" and KEY not in f"{s!r}"
    assert s.reveal() == KEY
    assert KEY not in repr([s]) and KEY not in repr({"k": s})
