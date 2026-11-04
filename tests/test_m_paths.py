"""T-373 A3: file access only inside the roots; one test per refusal."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from llmhub.mcp_server import paths as paths_mod
from llmhub.mcp_server.paths import PathDenied, Roots


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Roots, Path, Path]:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "a.txt").write_text("hello")
    (outside / "secret.txt").write_text("nope")
    roots = Roots.build([root], tmp_path / "scratch" / "out")
    return roots, root, outside


def _denied(fn: object, *args: object, **kw: object) -> str:
    with pytest.raises(PathDenied) as exc:
        fn(*args, **kw)  # type: ignore[operator]
    return exc.value.code


# --- input --------------------------------------------------------------------------------
def test_input_ok(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    p = roots.resolve_input(str(root / "a.txt"))
    assert roots.read_text(p, 100) == "hello"


def test_relative_path(tree: tuple[Roots, Path, Path]) -> None:
    roots, _, _ = tree
    assert _denied(roots.resolve_input, "a.txt") == "path_not_absolute"
    assert _denied(roots.resolve_output, "out.txt", overwrite=False) == "path_not_absolute"


def test_dotdot_outward(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    code = _denied(roots.resolve_input, f"{root}/../outside/secret.txt")
    assert code in {"dot_component", "outside_roots"}


def test_plain_outside(tree: tuple[Roots, Path, Path]) -> None:
    roots, _, outside = tree
    assert _denied(roots.resolve_input, str(outside / "secret.txt")) == "outside_roots"


def test_file_symlink_outward(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, outside = tree
    (root / "link.txt").symlink_to(outside / "secret.txt")
    assert _denied(roots.resolve_input, str(root / "link.txt")) == "outside_roots"


def test_dir_symlink_outward(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, outside = tree
    (root / "dir").symlink_to(outside)
    assert _denied(roots.resolve_input, str(root / "dir" / "secret.txt")) == "outside_roots"


@pytest.mark.parametrize("rel", [".env", ".git/config", "sub/.ssh/id"])
def test_dot_components(tree: tuple[Roots, Path, Path], rel: str) -> None:
    roots, root, _ = tree
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    assert _denied(roots.resolve_input, str(target)) == "dot_component"


def test_symlink_to_dotfile_inside(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / ".env").write_text("K=V")
    (root / "link").symlink_to(root / ".env")
    assert _denied(roots.resolve_input, str(root / "link")) == "dot_component"


def test_dotdir_then_dotdot(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / ".git").mkdir()
    assert _denied(roots.resolve_input, f"{root}/.git/../a.txt") == "dot_component"


def test_directory_is_not_file(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / "d").mkdir()
    assert _denied(roots.resolve_input, str(root / "d")) == "not_regular_file"


def test_fifo_is_not_file(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    os.mkfifo(root / "fifo")
    assert _denied(roots.resolve_input, str(root / "fifo")) == "not_regular_file"


def test_missing_input(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    assert _denied(roots.resolve_input, str(root / "missing.txt")) == "not_found"


def test_input_over_limit_by_size(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    p = roots.resolve_input(str(root / "a.txt"))
    assert _denied(roots.read_text, p, 4) == "input_too_large"
    assert roots.read_text(p, 5) == "hello"


def test_not_utf8(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / "bin.dat").write_bytes(b"\xff\xfe\x00bad")
    p = roots.resolve_input(str(root / "bin.dat"))
    assert _denied(roots.read_text, p, 100) == "not_utf8"


def test_root_with_dot_in_its_own_path(tmp_path: Path) -> None:
    root = tmp_path / ".state" / "root"
    root.mkdir(parents=True)
    (root / "a.txt").write_text("ok")
    roots = Roots.build([root], tmp_path / "s")
    assert roots.read_text(roots.resolve_input(str(root / "a.txt")), 10) == "ok"


def test_denied_message_has_no_outside_path(tree: tuple[Roots, Path, Path]) -> None:
    roots, _, outside = tree
    with pytest.raises(PathDenied) as exc:
        roots.resolve_input(str(outside / "secret.txt"))
    assert str(outside) not in str(exc.value) and "secret" not in str(exc.value)


def test_null_byte(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    assert _denied(roots.resolve_input, f"{root}/a\x00.txt") == "invalid_path"


def test_read_rechecks_after_open(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, outside = tree
    p = roots.resolve_input(str(root / "a.txt"))
    # swap the checked file for a symlink outward before reading (TOCTOU)
    os.unlink(p)
    os.symlink(outside / "secret.txt", p)
    assert _denied(roots.read_text, p, 100) == "symlink"


# --- output -------------------------------------------------------------------------------
def test_output_ok_new_file_0600(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    out = roots.resolve_output(str(root / "out.txt"), overwrite=False)
    roots.write_text(out, "result", overwrite=False)
    assert out.read_text() == "result"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_output_exists_without_overwrite(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    assert _denied(roots.resolve_output, str(root / "a.txt"), overwrite=False) == "exists"


def test_output_exists_with_overwrite(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    out = roots.resolve_output(str(root / "a.txt"), overwrite=True)
    roots.write_text(out, "new", overwrite=True)
    assert (root / "a.txt").read_text() == "new"


def test_output_appeared_between_check_and_write(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    out = roots.resolve_output(str(root / "late.txt"), overwrite=False)
    out.write_text("someone else")
    assert _denied(roots.write_text, out, "mine", overwrite=False) == "exists"
    assert out.read_text() == "someone else"
    assert [p.name for p in root.iterdir() if p.name.startswith(".")] == []


def test_output_symlink_even_inside(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / "o.txt").symlink_to(root / "a.txt")
    assert _denied(roots.resolve_output, str(root / "o.txt"), overwrite=True) == "symlink"
    (root / "dangling").symlink_to(root / "nothing.txt")
    assert _denied(roots.resolve_output, str(root / "dangling"), overwrite=False) == "symlink"


def test_output_parent_missing(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    code = _denied(roots.resolve_output, str(root / "nodir" / "o.txt"), overwrite=False)
    assert code == "parent_missing"


def test_output_outside(tree: tuple[Roots, Path, Path]) -> None:
    roots, _, outside = tree
    code = _denied(roots.resolve_output, str(outside / "o.txt"), overwrite=False)
    assert code == "outside_roots"


def test_output_via_dir_symlink_outward(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, outside = tree
    (root / "d").symlink_to(outside)
    code = _denied(roots.resolve_output, str(root / "d" / "o.txt"), overwrite=False)
    assert code == "outside_roots"


def test_output_dot_name_and_dot_dir(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / ".git").mkdir()
    assert _denied(roots.resolve_output, str(root / ".env"), overwrite=False) == "dot_component"
    code = _denied(roots.resolve_output, str(root / ".git" / "o"), overwrite=False)
    assert code == "dot_component"


def test_output_directory_target(tree: tuple[Roots, Path, Path]) -> None:
    roots, root, _ = tree
    (root / "d").mkdir()
    assert _denied(roots.resolve_output, str(root / "d"), overwrite=True) == "not_regular_file"


def test_write_atomic_no_temp_left_on_error(
    tree: tuple[Roots, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, root, _ = tree
    out = roots.resolve_output(str(root / "o.txt"), overwrite=False)

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(paths_mod.os, "fsync", boom)
    with pytest.raises(OSError):
        roots.write_text(out, "x" * 100, overwrite=False)
    assert sorted(p.name for p in root.iterdir()) == ["a.txt"]


# --- scratch ------------------------------------------------------------------------------
def test_scratch_is_root_and_0700(tree: tuple[Roots, Path, Path], tmp_path: Path) -> None:
    roots, _, _ = tree
    scratch = tmp_path / "scratch" / "out"
    assert scratch.is_dir() and stat.S_IMODE(scratch.stat().st_mode) == 0o700
    assert roots.scratch == scratch.resolve() and roots.scratch in roots.roots
    out = roots.resolve_output(str(scratch / "x.txt"), overwrite=False)
    roots.write_text(out, "s", overwrite=False)
    assert roots.read_text(roots.resolve_input(str(out)), 10) == "s"


def test_scratch_under_dot_dir_of_another_root(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    scratch = root / ".cache" / "mcp"
    root.mkdir()
    roots = Roots.build([root], scratch)
    out = roots.resolve_output(str(scratch / "r.txt"), overwrite=False)
    assert out.parent == scratch.resolve()


@pytest.mark.parametrize(("name", "expected"), [
    ("../x", "_x"), ("a/b.txt", "a_b.txt"), ("job-1_2.rejected.txt", "job-1_2.rejected.txt"),
    ("..", "_"), (".hidden", "hidden"),
])  # fmt: skip
def test_scratch_file_filters(tree: tuple[Roots, Path, Path], name: str, expected: str) -> None:
    roots, _, _ = tree
    p = roots.scratch_file(name)
    assert p.parent == roots.scratch and p.name == expected
