"""File access confined to the allowed roots (T-373 plan §2.4, §3.3).

Rules: absolute paths only; no `..`; no component below the root may start with a dot (checked
on the normalized path and again after `realpath`); inputs are regular files; outputs are
written atomically with mode 0600 and never replace an existing file unless asked. Error text
carries only a fixed code, never a path.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import secrets
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("llmhub.mcp")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


class PathDenied(Exception):
    """`code`: path_not_absolute | invalid_path | outside_roots | dot_component |
    not_regular_file | not_found | exists | parent_missing | symlink | not_utf8 |
    input_too_large | scratch_unavailable."""

    def __init__(self, code: str) -> None:
        super().__init__(f"path denied: {code}")
        self.code = code


def _tail_has_dot(path: str, root: str) -> bool:
    rel = os.path.relpath(path, root)
    if rel == ".":
        return False
    return any(part.startswith(".") for part in rel.split(os.sep))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class Roots:
    roots: tuple[Path, ...]  # realpath, scratch included
    scratch: Path
    lexical: tuple[Path, ...] = ()  # roots as given (normalized), for the first-stage check

    @classmethod
    def build(cls, roots: Sequence[Path], scratch: Path) -> Roots:
        scratch_abs = Path(os.path.normpath(os.path.abspath(scratch)))
        try:
            _make_private_dir(scratch_abs)
        except OSError:
            log.warning("llmhub-mcp: scratch directory is not available")
        given = [Path(os.path.normpath(os.path.abspath(r))) for r in roots] + [scratch_abs]
        real = [Path(os.path.realpath(r)) for r in given]
        return cls(
            roots=tuple(dict.fromkeys(real)),
            scratch=Path(os.path.realpath(scratch_abs)),
            lexical=tuple(dict.fromkeys(given + real)),
        )

    # --- matching -----------------------------------------------------------------------
    @staticmethod
    def _best(path: str, candidates: Sequence[Path]) -> str | None:
        hits = [str(r) for r in candidates if _under(path, str(r))]
        return max(hits, key=len) if hits else None

    def _check_lexical(self, raw: str) -> str:
        if "\x00" in raw:
            raise PathDenied("invalid_path")
        if not os.path.isabs(raw):
            raise PathDenied("path_not_absolute")
        if ".." in raw.split(os.sep):
            raise PathDenied("dot_component")
        norm = os.path.normpath(raw)
        root = self._best(norm, self.lexical)
        if root is None:
            raise PathDenied("outside_roots")
        if _tail_has_dot(norm, root):
            raise PathDenied("dot_component")
        return norm

    def _check_real(self, real: str) -> str:
        root = self._best(real, self.roots)
        if root is None:
            raise PathDenied("outside_roots")
        if _tail_has_dot(real, root):
            raise PathDenied("dot_component")
        return real

    # --- public -------------------------------------------------------------------------
    def resolve_input(self, raw: str) -> Path:
        norm = self._check_lexical(raw)
        real = self._check_real(os.path.realpath(norm))
        try:
            st = os.stat(real)
        except FileNotFoundError:
            raise PathDenied("not_found") from None
        except OSError:
            raise PathDenied("not_regular_file") from None
        if not stat.S_ISREG(st.st_mode):
            raise PathDenied("not_regular_file")
        return Path(real)

    def resolve_output(self, raw: str, *, overwrite: bool) -> Path:
        norm = self._check_lexical(raw)
        if os.path.islink(norm):
            raise PathDenied("symlink")
        parent = os.path.realpath(os.path.dirname(norm))
        if not os.path.isdir(parent):
            raise PathDenied("parent_missing")
        target = os.path.join(parent, os.path.basename(norm))
        self._check_real(target)
        if os.path.lexists(target):
            if os.path.islink(target):
                raise PathDenied("symlink")
            if not os.path.isfile(target):
                raise PathDenied("not_regular_file")
            if not overwrite:
                raise PathDenied("exists")
        return Path(target)

    def read_text(self, path: Path, limit: int) -> str:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathDenied("symlink") from None
            if exc.errno == errno.ENOENT:
                raise PathDenied("not_found") from None
            raise PathDenied("not_regular_file") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise PathDenied("not_regular_file")
            self._check_real(os.readlink(f"/proc/self/fd/{fd}"))
            if st.st_size > limit:
                raise PathDenied("input_too_large")
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(fd, min(1 << 20, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > limit:
                raise PathDenied("input_too_large")
        finally:
            os.close(fd)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            raise PathDenied("not_utf8") from None

    def write_text(self, path: Path, text: str, *, overwrite: bool) -> None:
        """Atomic write: temp file (O_EXCL|O_NOFOLLOW, 0600) in the same directory, then
        `link` (no overwrite, fails if the target appeared) or `replace` (overwrite)."""
        parent, name = os.path.dirname(path), os.path.basename(path)
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            self._check_real(os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), name))
            tmp = f".llmhub-mcp-{secrets.token_hex(8)}.tmp"
            fd = os.open(
                tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                dir_fd=dir_fd,
            )  # fmt: skip
            try:
                try:
                    data = text.encode("utf-8")
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view) :]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if overwrite:
                    try:
                        st = os.lstat(name, dir_fd=dir_fd)
                    except FileNotFoundError:
                        st = None
                    if st is not None and stat.S_ISLNK(st.st_mode):
                        raise PathDenied("symlink")
                    if st is not None and not stat.S_ISREG(st.st_mode):
                        raise PathDenied("not_regular_file")
                    os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                else:
                    try:
                        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                                follow_symlinks=False)  # fmt: skip
                    except FileExistsError:
                        raise PathDenied("exists") from None
            finally:
                try:
                    os.unlink(tmp, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(dir_fd)

    def scratch_file(self, name: str) -> Path:
        safe = _SAFE_NAME.sub("_", name).lstrip(".") or "_"
        try:
            _make_private_dir(self.scratch)
        except OSError:
            raise PathDenied("scratch_unavailable") from None
        return self.scratch / safe


def _make_private_dir(path: Path) -> None:
    if path.is_dir():
        return
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
