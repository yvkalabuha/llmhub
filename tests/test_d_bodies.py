import stat
from pathlib import Path

from llmhub.task_queue.bodies import BodyStore


def test_permissions_and_atomic_write(tmp_path: Path) -> None:
    store = BodyStore(tmp_path / "b")
    assert stat.S_IMODE((tmp_path / "b").stat().st_mode) == 0o700
    p = store.write_request("j1", b"hello", {"client_id": "c"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert store.read_request("j1") == b"hello"
    assert not list((tmp_path / "b").glob("*.tmp"))


def test_iter_recoverable_skips_completed(tmp_path: Path) -> None:
    store = BodyStore(tmp_path / "b")
    store.write_request("j1", b"a", {"client_id": "c1"})
    store.write_request("j2", b"b", {"client_id": "c2"})
    store.write_result("j2", b"r")
    assert list(store.iter_recoverable()) == [("j1", {"client_id": "c1"}, b"a")]


def test_purge_and_discard(tmp_path: Path) -> None:
    store = BodyStore(tmp_path / "b")
    store.write_request("j1", b"a", {})
    store.write_request("j2", b"b", {})
    store.discard("j1")
    assert not (tmp_path / "b" / "j1.req").exists()
    store.purge(older_than=1e12)
    assert not list((tmp_path / "b").iterdir())
