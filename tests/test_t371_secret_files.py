"""T-371 C2: secrets from files (`NAME_FILE`), never echoed; docker inspect shows paths only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llmhub.cloud_manager.instance_env import load_instance_secrets
from llmhub.configuration.loader import (
    INSTANCE_SECRET_ENV,
    SECRET_ENV,
    WEIGHTS_SECRET_ENV,
    ConfigError,
    load_config,
    load_instance_secret,
    load_label_secret,
    load_weights_secret,
    read_secret_file,
)

GOOD = "s3cr3t-" + "q" * 40


def _file(tmp: Path, name: str, text: str, mode: int = 0o640) -> Path:
    p = tmp / name
    p.write_text(text)
    p.chmod(mode)
    return p


def test_file_variant_reads_first_line(tmp_path: Path) -> None:
    p = _file(tmp_path, "label", GOOD + "\nsecond line ignored\n")
    assert load_label_secret({SECRET_ENV + "_FILE": str(p)}) == GOOD.encode()
    assert read_secret_file(p) == GOOD.encode()


def test_env_variant_still_works() -> None:
    assert load_label_secret({SECRET_ENV: GOOD}) == GOOD.encode()


def test_both_set_is_an_error() -> None:
    with pytest.raises(ConfigError, match="both"):
        load_label_secret({SECRET_ENV: GOOD, SECRET_ENV + "_FILE": "/x"})


def test_neither_set_is_an_error() -> None:
    with pytest.raises(ConfigError, match="not set"):
        load_weights_secret({})


@pytest.mark.parametrize("text", ["", "\n", "short\n", "   \n" + GOOD])
def test_empty_or_short_file_rejected_without_content(tmp_path: Path, text: str) -> None:
    p = _file(tmp_path, "w", text)
    with pytest.raises(ConfigError) as ei:
        load_weights_secret({WEIGHTS_SECRET_ENV + "_FILE": str(p)})
    assert "short" not in str(ei.value) and GOOD not in str(ei.value)


def test_symlink_rejected(tmp_path: Path) -> None:
    real = _file(tmp_path, "real", GOOD)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ConfigError, match="regular file"):
        load_instance_secret({INSTANCE_SECRET_ENV + "_FILE": str(link)})


def test_directory_and_missing_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        read_secret_file(tmp_path)
    with pytest.raises(ConfigError):
        read_secret_file(tmp_path / "missing")


def test_world_readable_file_rejected(tmp_path: Path) -> None:
    p = _file(tmp_path, "w", GOOD, mode=0o644)
    with pytest.raises(ConfigError, match="mode"):
        read_secret_file(p)


def test_error_text_never_contains_the_secret(tmp_path: Path) -> None:
    p = _file(tmp_path, "w", GOOD, mode=0o666)
    with pytest.raises(ConfigError) as ei:
        read_secret_file(p)
    assert GOOD not in str(ei.value)


def test_instance_secrets_must_differ_in_file_variant(tmp_path: Path) -> None:
    a = _file(tmp_path, "a", GOOD)
    b = _file(tmp_path, "b", GOOD + "\n")
    cfg = load_config(_cfg(tmp_path))
    env = {INSTANCE_SECRET_ENV + "_FILE": str(a), WEIGHTS_SECRET_ENV + "_FILE": str(b)}
    with pytest.raises(ConfigError, match="must differ"):
        load_instance_secrets(cfg, env)
    c = _file(tmp_path, "c", "other-" + "z" * 40)
    env[WEIGHTS_SECRET_ENV + "_FILE"] = str(c)
    s = load_instance_secrets(cfg, env)
    assert s.instance == GOOD.encode() and s.weights != s.instance


def _cfg(tmp: Path) -> Path:
    p = tmp / "config.yaml"
    p.write_text(f"data_dir: {tmp / 'data'}\npools: {{}}\n")
    return p


def test_fifo_is_not_read(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    fifo.chmod(0o640)
    with pytest.raises(ConfigError, match="regular file"):
        read_secret_file(fifo)  # must not block on open
