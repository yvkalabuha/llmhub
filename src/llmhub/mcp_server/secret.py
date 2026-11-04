"""Client key holder and loader (T-373 plan §3.2): the value is never printed."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)


class KeyUnavailable(Exception):
    """The key cannot be loaded; `reason` is a fixed word, never the path or the content."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"client key unavailable: {reason}")
        self.reason = reason


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def load_key(key_env: str | None, key_file: Path) -> Secret:
    if key_env is not None and key_env.strip():
        return Secret(key_env.strip())
    try:
        fd = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise KeyUnavailable("missing") from None
    except OSError:
        # ELOOP (symlink with O_NOFOLLOW) and similar
        raise KeyUnavailable("not_regular") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise KeyUnavailable("not_regular")
        if st.st_uid != os.getuid():
            raise KeyUnavailable("bad_owner")
        if st.st_mode & 0o077:
            raise KeyUnavailable("bad_mode")
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace", closefd=False) as fh:
            value = _first_line(fh.read(64 * 1024))
    finally:
        os.close(fd)
    if not value:
        raise KeyUnavailable("empty")
    return Secret(value)
