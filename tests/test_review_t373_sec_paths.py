"""T-373 review cycle 1 (security): extra probes of Roots and the key loader.

Confirmed defects are `xfail(strict=True)`; the rest pin behaviour that must not regress.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from llmhub.mcp_server.paths import PathDenied, Roots
from llmhub.mcp_server.secret import KeyUnavailable, load_key


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Roots, Path, Path]:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET")
    return Roots.build([root], tmp_path / "scratch"), root, outside


def _code(fn: object, *args: object, **kw: object) -> str:
    with pytest.raises(PathDenied) as exc:
        fn(*args, **kw)  # type: ignore[operator]
    return exc.value.code


# --- input ---------------------------------------------------------------------------------
def test_parent_swapped_to_symlink_after_resolve_is_caught(
    tree: tuple[Roots, Path, Path],
) -> None:
    roots, root, outside = tree
    (root / "d").mkdir()
    (root / "d" / "secret.txt").write_text("inside")
    p = roots.resolve_input(str(root / "d" / "secret.txt"))
    (root / "d" / "secret.txt").unlink()
    (root / "d").rmdir()
    (root / "d").symlink_to(outside)  # TOCTOU: intermediate component now points outside
    assert _code(roots.read_text, p, 1000) == "outside_roots"


def test_final_symlink_swapped_after_resolve_is_refused(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, outside = tree
    (root / "a.txt").write_text("x")
    p = roots.resolve_input(str(root / "a.txt"))
    (root / "a.txt").unlink()
    (root / "a.txt").symlink_to(outside / "secret.txt")
    assert _code(roots.read_text, p, 1000) == "symlink"


@pytest.mark.parametrize("raw", ["/root/./a.txt", "//x", "/r/a.txt/.", "/r/.\x00"])
def test_odd_lexical_forms_are_refused(tree: tuple[Roots, Path, Path], raw: str) -> None:
    roots, _, _ = tree
    with pytest.raises(PathDenied):
        roots.resolve_input(raw)


def test_dot_component_via_single_dot_inside_root(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / "a.txt").write_text("x")
    # `root/./a.txt` normalizes to root/a.txt: allowed (no hidden name involved)
    assert roots.resolve_input(f"{root}/./a.txt") == root / "a.txt"


def test_dev_null_symlink_inside_root_is_not_regular(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / "z").symlink_to("/dev/zero")
    assert _code(roots.resolve_input, str(root / "z")) in {"outside_roots", "not_regular_file"}


def test_proc_symlink_inside_root_is_outside(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / "env").symlink_to("/proc/self/environ")
    assert _code(roots.resolve_input, str(root / "env")) == "outside_roots"


def test_proc_environ_refused_even_with_root_slash(tmp_path: Path) -> None:
    roots = Roots.build([Path("/")], tmp_path / "scratch")
    with pytest.raises(PathDenied):
        p = roots.resolve_input("/proc/self/environ")
        roots.read_text(p, 400_000)


def test_hard_link_to_outside_file_is_readable_documented(tree: tuple[Roots, Path, Path]) -> None:
    """Known limitation (low): a hard link inside a root bypasses realpath. The agent needs to
    own the file to link it (protected_hardlinks), so it could read it anyway."""
    roots, root, outside = tree
    os.link(outside / "secret.txt", root / "h.txt")
    assert roots.read_text(roots.resolve_input(str(root / "h.txt")), 100) == "TOPSECRET"


# --- output --------------------------------------------------------------------------------
def test_overwrite_of_hard_link_does_not_write_through(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, outside = tree
    os.link(outside / "secret.txt", root / "h.txt")
    p = roots.resolve_output(str(root / "h.txt"), overwrite=True)
    roots.write_text(p, "new", overwrite=True)
    assert (outside / "secret.txt").read_text() == "TOPSECRET"
    assert (root / "h.txt").read_text() == "new"


def test_symlink_planted_after_resolve_overwrite_is_refused(
    tree: tuple[Roots, Path, Path],
) -> None:
    roots, root, outside = tree
    p = roots.resolve_output(str(root / "o.txt"), overwrite=True)
    (root / "o.txt").symlink_to(outside / "secret.txt")
    assert _code(roots.write_text, p, "x", overwrite=True) == "symlink"
    assert (outside / "secret.txt").read_text() == "TOPSECRET"
    assert not [n for n in os.listdir(root) if n.endswith(".tmp")]


def test_symlink_planted_after_resolve_no_overwrite_is_exists(
    tree: tuple[Roots, Path, Path],
) -> None:
    roots, root, outside = tree
    p = roots.resolve_output(str(root / "o.txt"), overwrite=False)
    (root / "o.txt").symlink_to(outside / "new.txt")
    assert _code(roots.write_text, p, "x", overwrite=False) == "exists"
    assert not (outside / "new.txt").exists()


def test_parent_swapped_after_resolve_output_is_caught(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, outside = tree
    (root / "d").mkdir()
    p = roots.resolve_output(str(root / "d" / "o.txt"), overwrite=False)
    (root / "d").rmdir()
    (root / "d").symlink_to(outside)
    assert _code(roots.write_text, p, "x", overwrite=False) == "outside_roots"
    assert not (outside / "o.txt").exists()


def test_output_mode_0600_despite_permissive_umask(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    old = os.umask(0)
    try:
        p = roots.resolve_output(str(root / "o.txt"), overwrite=False)
        roots.write_text(p, "x", overwrite=False)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(root / "o.txt").st_mode) == 0o600


def test_existing_scratch_dir_is_made_private(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    os.chmod(scratch, 0o777)
    roots = Roots.build([], scratch)
    roots.scratch_file("t.txt")
    assert stat.S_IMODE(os.stat(scratch).st_mode) & 0o077 == 0


def test_scratch_name_cannot_escape(tree: tuple[Roots, Path, Path]) -> None:
    roots, _, _ = tree
    for name in ("../../etc/passwd", "/abs", "..", ".hidden", "a/b"):
        p = roots.scratch_file(name)
        assert p.parent == roots.scratch and not p.name.startswith(".")


# --- key -----------------------------------------------------------------------------------
def test_key_errors_carry_no_path_or_content(tmp_path: Path) -> None:
    f = tmp_path / "sekrit-path" / "key"
    f.parent.mkdir()
    f.write_text("sk-VERYSECRET\n")
    os.chmod(f, 0o644)
    with pytest.raises(KeyUnavailable) as exc:
        load_key(None, f)
    text = f"{exc.value!s} {exc.value!r} {exc.value.args}"
    assert "sekrit" not in text and "VERYSECRET" not in text


def test_key_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    f = tmp_path / "key"
    os.mkfifo(f, 0o600)
    with pytest.raises(KeyUnavailable) as exc:
        load_key(None, f)
    assert exc.value.reason == "not_regular"


def test_key_group_readable_refused(tmp_path: Path) -> None:
    f = tmp_path / "key"
    f.write_text("k")
    os.chmod(f, 0o640)
    with pytest.raises(KeyUnavailable) as exc:
        load_key(None, f)
    assert exc.value.reason == "bad_mode"
