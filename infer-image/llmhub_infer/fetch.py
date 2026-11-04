"""Weights downloader: source chain, sha256, Range resume, parallel segments, failover."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .plan import FileSpec, SourceSpec, WeightsPlan
from .speed import SpeedMonitor

log = logging.getLogger("llmhub_infer.fetch")

SEGMENT_CHUNK = 128 * 1024 * 1024
MAX_REDIRECTS = 5
DISK_MARGIN = 1 << 30
MB = 1_000_000
_CR_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_REDIRECTS = (301, 302, 303, 307, 308)


class FetchError(Exception):
    """Download failed. `.reason`: http_4xx|http_5xx|rate_limited|network|slow|sha_mismatch|size_mismatch|
    disk|exhausted|deadline. `.causes`: for `exhausted`, the sorted distinct per-source
    failover reasons (review r2, R2-1: the controller tells config errors from host faults)."""

    def __init__(self, reason: str, detail: str = "", causes: tuple[str, ...] = ()) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.causes = causes


class _Fail(Exception):
    """Failure of one attempt on one source."""

    def __init__(self, reason: str, retry: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry = retry


class _Stopped(Exception):
    """A sibling segment worker already failed."""


@dataclass
class Progress:
    phase: str
    file: str | None
    source: str | None
    done: int
    total: int
    bps: float
    failovers: list[dict[str, str]]
    error: str | None
    eta_s: float | None = None
    sources_left: int = 0


@dataclass
class _Target:
    """Cached redirect target (CDN) for one file on one source."""

    url: str | None = None


@dataclass
class _SegState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    done: set[int] = field(default_factory=set)


def _default_transport(source: SourceSpec) -> httpx.BaseTransport:
    ctx = (
        ssl.create_default_context(cadata=source.ca_pem)
        if source.ca_pem
        else ssl.create_default_context()
    )
    return httpx.HTTPTransport(
        verify=ctx, limits=httpx.Limits(max_connections=max(1, source.segments))
    )


def _hash_file(path: Path, upto: int, digest: hashlib._Hash) -> None:
    left = upto
    with path.open("rb") as f:
        while True:
            n = min(8 * 1024 * 1024, left)
            if n == 0:
                break
            buf = f.read(n)
            if not buf:
                break
            digest.update(buf)
            left -= len(buf)


class Fetcher:
    def __init__(
        self,
        plan: WeightsPlan,
        dest: Path,
        secrets: Mapping[str, str],
        on_progress: Callable[[Progress], None],
        clock: Callable[[], float] = time.monotonic,
        transport_factory: Callable[[SourceSpec], httpx.BaseTransport] | None = None,
        *,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        chunk_size: int = SEGMENT_CHUNK,
        disk_margin: int = DISK_MARGIN,
        progress_interval_s: float = 1.0,
    ) -> None:
        self._plan = plan
        self._dest = dest
        self._secrets = dict(secrets)
        self._on_progress = on_progress
        self._clock = clock
        self._factory = transport_factory or _default_transport
        self._wall = wall
        self._sleep = sleep
        self._chunk = chunk_size
        self._margin = disk_margin
        self._interval = progress_interval_s
        self._src = 0
        self._failovers: list[dict[str, str]] = []
        self._base_done = 0
        self._cur_done = 0
        self._max_done = 0
        self._total = sum(f.size for f in plan.files)
        self._phase = "downloading"
        self._file: str | None = None
        self._last_emit = float("-inf")
        self._bps = 0.0
        self._plock = threading.Lock()

    # ----------------------------------------------------------------- progress
    def _emit(self, *, force: bool = False, error: str | None = None) -> None:
        with self._plock:
            now = self._clock()
            if not force and now - self._last_emit < self._interval:
                return
            self._last_emit = now
            done = min(self._total, max(self._max_done, self._base_done + self._cur_done))
            self._max_done = done
            src = (
                self._plan.sources[self._src].name if self._src < len(self._plan.sources) else None
            )
            eta = (self._total - done) / self._bps if self._bps > 0 else None
            p = Progress(
                phase=self._phase,
                file=self._file,
                source=src,
                done=done,
                total=self._total,
                bps=self._bps,
                failovers=[dict(x) for x in self._failovers],
                error=error,
                eta_s=eta,
                sources_left=max(0, len(self._plan.sources) - self._src - 1),
            )
            self._on_progress(p)

    def _check_deadline(self) -> None:
        d = self._plan.deadline_unix
        if d is not None and self._wall() >= d:
            raise FetchError("deadline")

    # --------------------------------------------------------------------- run
    def run(self) -> dict[str, Path]:
        order = sorted(self._plan.files, key=lambda f: (f.id != "draft", f.id != "model"))
        out: dict[str, Path] = {}
        self._dest.mkdir(parents=True, exist_ok=True)
        try:
            for spec in order:
                self._file = spec.id
                self._cur_done = 0
                out[spec.id] = self._fetch_file(spec)
                self._base_done += spec.size
                self._cur_done = 0
            self._phase, self._file, self._bps = "done", None, 0.0
            self._emit(force=True)
        except FetchError as e:
            self._phase, self._bps = "failed", 0.0
            self._emit(force=True, error=e.reason)
            raise
        return out

    def _paths(self, spec: FileSpec) -> tuple[Path, Path, Path, Path]:
        final = self._dest / f"{spec.id}.gguf"
        return (
            final,
            final.with_name(final.name + ".sha256"),
            final.with_name(final.name + ".part"),
            final.with_name(final.name + ".part.json"),
        )

    def _complete(self, spec: FileSpec) -> bool:
        final, marker, _, _ = self._paths(spec)
        try:
            return (
                final.stat().st_size == spec.size
                and marker.read_text(encoding="ascii").strip() == spec.sha256
            )
        except OSError:
            return False

    def _fetch_file(self, spec: FileSpec) -> Path:
        final = self._paths(spec)[0]
        if self._complete(spec):
            self._cur_done = 0
            return final
        need = self._missing_bytes(spec) + self._margin
        if shutil.disk_usage(self._dest).free < need:
            raise FetchError("disk")
        self._emit(force=True)
        while self._src < len(self._plan.sources):
            source = self._plan.sources[self._src]
            reason = "network"
            for attempt in range(self._plan.retries_per_source + 1):
                self._check_deadline()
                try:
                    if source.segments > 1:
                        self._attempt_segmented(spec, source)
                    else:
                        self._attempt_single(spec, source)
                    return final
                except _Fail as f:
                    reason = f.reason
                    log.warning("source %s failed for %s: %s", source.name, spec.id, f.reason)
                    if f.retry and attempt < self._plan.retries_per_source:
                        self._sleep(min(2.0, 1.0 + attempt))
                        continue
                    break
            self._failovers.append({"source": source.name, "file": spec.id, "reason": reason})
            self._src += 1
            self._bps = 0.0
            self._emit(force=True)
        causes = tuple(sorted({x["reason"] for x in self._failovers}))
        raise FetchError("exhausted", causes=causes)

    def _missing_bytes(self, spec: FileSpec) -> int:
        """Bytes still to download. A segmented `.part` is sparse at full size, so the done
        chunks come from the sidecar; otherwise `.part` is a contiguous prefix."""
        _, _, part, side = self._paths(spec)
        if side.exists():
            done = self._read_sidecar(spec, side)
            if done is not None:
                n_chunks = -(-spec.size // self._chunk)
                have = sum(self._chunk_len(spec, i) for i in done if 0 <= i < n_chunks)
                return max(0, spec.size - have)
            return spec.size  # unusable sidecar: the attempt keeps at most a prefix
        have = part.stat().st_size if part.exists() else 0
        return max(0, spec.size - min(have, spec.size))

    def _finish(self, spec: FileSpec, h: hashlib._Hash) -> None:
        final, marker, part, side = self._paths(spec)
        size = part.stat().st_size
        if size != spec.size:
            part.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
            raise _Fail("size_mismatch", False)
        if h.hexdigest() != spec.sha256:
            part.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
            raise _Fail("sha_mismatch", False)
        os.replace(part, final)
        marker.write_text(spec.sha256, encoding="ascii")
        side.unlink(missing_ok=True)
        self._cur_done = spec.size
        self._phase = "downloading"
        self._emit(force=True)

    # ------------------------------------------------------------------- http
    def _url(self, source: SourceSpec, spec: FileSpec) -> str:
        rel = "/".join(quote(p, safe="") for p in source.paths[spec.id].split("/"))
        return source.base_url + rel

    def _headers(self, source: SourceSpec, url: str) -> dict[str, str]:
        parts = urlsplit(url)
        base = urlsplit(source.base_url)
        token: str | None = None
        if source.auth == "weights_token" and parts.netloc == base.netloc:
            token = self._secrets.get("LLMHUB_WEIGHTS_TOKEN")
        elif parts.hostname == "huggingface.co":
            token = self._secrets.get("HF_TOKEN")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _send(
        self,
        client: httpx.Client,
        source: SourceSpec,
        url: str,
        extra: dict[str, str],
    ) -> tuple[httpx.Response, str]:
        """Follow redirects by hand; credentials are chosen per host, never carried over."""
        for _ in range(MAX_REDIRECTS + 1):
            if not url.startswith("https://"):
                raise _Fail("network", False)
            # identity: Range offsets and sha256 refer to the stored bytes, never to a
            # content-coded representation (the body is read with iter_raw)
            headers = {**extra, "Accept-Encoding": "identity", **self._headers(source, url)}
            resp = client.send(client.build_request("GET", url, headers=headers), stream=True)
            if resp.status_code in _REDIRECTS and resp.headers.get("location"):
                loc = resp.headers["location"]
                resp.close()
                url = urljoin(url, loc)
                continue
            if resp.headers.get("content-encoding", "identity").strip().lower() != "identity":
                resp.close()
                raise _Fail("network", False)  # the source ignored identity: next source
            return resp, url
        raise _Fail("network", False)

    @contextlib.contextmanager
    def _open(
        self,
        client: httpx.Client,
        source: SourceSpec,
        spec: FileSpec,
        extra: dict[str, str],
        target: _Target,
    ) -> Iterator[httpx.Response]:
        base = self._url(source, spec)
        resp: httpx.Response | None = None
        try:
            if target.url:
                resp, _ = self._send(client, source, target.url, extra)
                if resp.status_code in (403, 410):
                    resp.close()
                    resp = None
                    target.url = None
            if resp is None:
                resp, final = self._send(client, source, base, extra)
                if final != base and resp.status_code < 300:
                    target.url = final
            yield resp
        except httpx.DecodingError as e:  # a content-coded body: next source
            raise _Fail("network", False) from e
        except httpx.TransportError as e:
            raise _Fail("network", True) from e
        finally:
            if resp is not None:
                resp.close()

    @staticmethod
    def _status_fail(resp: httpx.Response) -> _Fail | None:
        code = resp.status_code
        if code >= 500:
            return _Fail("http_5xx", True)
        if code in (408, 429):  # transient (timeout / rate limit), never a config error that holds the pool
            return _Fail("rate_limited", True)
        if code >= 400 or code < 200 or code >= 300:
            return _Fail("http_4xx" if code >= 400 else "network", False)
        return None

    def _client(self, source: SourceSpec) -> httpx.Client:
        p = self._plan
        return httpx.Client(
            transport=self._factory(source),
            timeout=httpx.Timeout(p.read_timeout_s, connect=p.connect_timeout_s),
            follow_redirects=False,
        )

    def _monitor(self, source: SourceSpec) -> SpeedMonitor:
        p = self._plan
        return SpeedMonitor(
            self._clock, p.speed_grace_s, p.speed_window_s, source.min_speed_mb_s * MB
        )

    def _can_drop_slow(self) -> bool:
        return self._src < len(self._plan.sources) - 1

    # -------------------------------------------------------- sidecar helpers
    def _read_sidecar(self, spec: FileSpec, side: Path) -> set[int] | None:
        try:
            d = json.loads(side.read_text(encoding="utf-8"))
            if d["size"] != spec.size or d["chunk"] != self._chunk:
                return None
            return {int(i) for i in d["done"]}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_sidecar(self, spec: FileSpec, side: Path, done: set[int]) -> None:
        tmp = side.with_name(side.name + ".tmp")
        tmp.write_text(
            json.dumps({"v": 1, "size": spec.size, "chunk": self._chunk, "done": sorted(done)}),
            encoding="utf-8",
        )
        os.replace(tmp, side)

    # ---------------------------------------------------------- single stream
    def _attempt_single(self, spec: FileSpec, source: SourceSpec) -> None:
        _, _, part, side = self._paths(spec)
        if side.exists():  # left by a segmented attempt: keep the contiguous prefix only
            done = self._read_sidecar(spec, side) or set()
            k = 0
            while k in done:
                k += 1
            with part.open("ab"):
                pass
            os.truncate(part, min(k * self._chunk, spec.size))
            side.unlink(missing_ok=True)
        pos = part.stat().st_size if part.exists() else 0
        if pos > spec.size:
            os.truncate(part, 0)
            pos = 0
        self._phase = "downloading"
        h = hashlib.sha256()
        if pos:
            _hash_file(part, pos, h)
        self._cur_done = pos
        monitor = self._monitor(source)
        target = _Target()
        client = self._client(source)
        try:
            retried_416 = False
            while pos < spec.size:
                extra = {"Range": f"bytes={pos}-"} if pos else {}
                with self._open(client, source, spec, extra, target) as resp:
                    code = resp.status_code
                    if code == 416 and pos > 0 and not retried_416:
                        retried_416 = True
                        os.truncate(part, 0)
                        pos, h = 0, hashlib.sha256()
                        self._cur_done = 0
                        continue
                    fail = self._status_fail(resp)
                    if fail:
                        raise fail
                    if code == 206:
                        m = _CR_RE.match(resp.headers.get("content-range", ""))
                        if not m or int(m.group(1)) != pos:
                            raise _Fail("network", True)
                        if m.group(3) != "*" and int(m.group(3)) != spec.size:
                            part.unlink(missing_ok=True)
                            raise _Fail("size_mismatch", False)
                        mode = "ab"
                    else:  # 200: the server ignored Range -> restart
                        pos, h = 0, hashlib.sha256()
                        self._cur_done = 0
                        mode = "wb"
                    with part.open(mode) as out:
                        try:
                            for data in resp.iter_raw():
                                if pos + len(data) > spec.size:
                                    out.close()
                                    part.unlink(missing_ok=True)
                                    raise _Fail("size_mismatch", False)
                                out.write(data)
                                h.update(data)
                                pos += len(data)
                                monitor.add(len(data))
                                self._bps = monitor.bps()
                                self._cur_done = pos
                                self._check_deadline()
                                if self._can_drop_slow() and monitor.too_slow():
                                    raise _Fail("slow", False)
                                self._emit()
                        except httpx.DecodingError as e:  # a content-coded body: next source
                            raise _Fail("network", False) from e
                        except httpx.TransportError as e:
                            raise _Fail("network", True) from e
                        except OSError as e:
                            raise FetchError("disk", e.__class__.__name__) from e
                    if pos < spec.size:  # clean end of body, but short
                        part.unlink(missing_ok=True)
                        raise _Fail("size_mismatch", False)
            self._phase = "verifying"
            self._finish(spec, h)
        finally:
            client.close()

    # --------------------------------------------------------------- segments
    def _attempt_segmented(self, spec: FileSpec, source: SourceSpec) -> None:
        _, _, part, side = self._paths(spec)
        chunk = self._chunk
        n_chunks = -(-spec.size // chunk)
        done = self._read_sidecar(spec, side) if side.exists() else None
        have = part.stat().st_size if part.exists() else 0
        if done is None:
            done = set()
            if 0 < have < spec.size:  # prefix from a single-stream attempt
                done = set(range(have // chunk))
            elif have >= spec.size:
                os.truncate(part, 0)
        if not part.exists():
            part.touch()
        os.truncate(part, spec.size)
        done = {i for i in done if i < n_chunks}
        self._write_sidecar(spec, side, done)
        st = _SegState(done=done)
        todo = deque(i for i in range(n_chunks) if i not in done)
        self._cur_done = sum(self._chunk_len(spec, i) for i in done)
        self._phase = "downloading"
        monitor = self._monitor(source)
        target = _Target()
        client = self._client(source)
        fd = os.open(part, os.O_WRONLY)
        errors: list[BaseException] = []
        try:

            def worker() -> None:
                try:
                    while not st.stop.is_set():
                        with st.lock:
                            if not todo:
                                return
                            idx = todo.popleft()
                        self._fetch_chunk(client, source, spec, idx, fd, monitor, target, st)
                        with st.lock:
                            st.done.add(idx)
                            self._write_sidecar(spec, side, st.done)
                except _Stopped:
                    return
                except BaseException as e:  # noqa: BLE001 - re-raised by the caller
                    with st.lock:
                        errors.append(e)
                    st.stop.set()

            if todo:
                with ThreadPoolExecutor(max_workers=source.segments) as pool:
                    futs = [pool.submit(worker) for _ in range(source.segments)]
                    for f in futs:
                        f.result()
        finally:
            os.close(fd)
            client.close()
        if errors:
            raise errors[0]
        self._phase = "verifying"
        self._bps = 0.0
        self._emit(force=True)
        h = hashlib.sha256()
        for _ in _iter_hash(part, h, self._check_deadline):
            pass
        self._finish(spec, h)

    def _chunk_len(self, spec: FileSpec, idx: int) -> int:
        return min(spec.size, (idx + 1) * self._chunk) - idx * self._chunk

    def _fetch_chunk(
        self,
        client: httpx.Client,
        source: SourceSpec,
        spec: FileSpec,
        idx: int,
        fd: int,
        monitor: SpeedMonitor,
        target: _Target,
        st: _SegState,
    ) -> None:
        a = idx * self._chunk
        length = self._chunk_len(spec, idx)
        b = a + length - 1
        got = 0
        retries = self._plan.retries_per_source
        for attempt in range(retries + 1):
            try:
                self._check_deadline()
                while got < length:
                    if st.stop.is_set():
                        raise _Stopped
                    extra = {"Range": f"bytes={a + got}-{b}"}
                    with self._open(client, source, spec, extra, target) as resp:
                        fail = self._status_fail(resp)
                        if fail:
                            raise fail
                        if resp.status_code != 206:
                            raise _Fail("network", False)
                        m = _CR_RE.match(resp.headers.get("content-range", ""))
                        if not m or int(m.group(1)) != a + got or int(m.group(2)) != b:
                            raise _Fail("network", True)
                        try:
                            for data in resp.iter_raw():
                                if got + len(data) > length:
                                    raise _Fail("size_mismatch", False)
                                os.pwrite(fd, data, a + got)
                                got += len(data)
                                with st.lock:
                                    monitor.add(len(data))
                                    self._bps = monitor.bps()
                                    self._cur_done += len(data)
                                    slow = self._can_drop_slow() and monitor.too_slow()
                                self._check_deadline()
                                if slow:
                                    raise _Fail("slow", False)
                                if st.stop.is_set():
                                    raise _Stopped
                                self._emit()
                        except httpx.DecodingError as e:  # a content-coded body: next source
                            raise _Fail("network", False) from e
                        except httpx.TransportError as e:
                            raise _Fail("network", True) from e
                    if got < length:
                        raise _Fail("size_mismatch", False)
                return
            except _Fail as f:
                if f.retry and attempt < retries:
                    self._sleep(min(2.0, 1.0 + attempt))
                    continue
                raise _Fail(f.reason, False) from None
            except OSError as e:
                raise FetchError("disk", e.__class__.__name__) from e


def _iter_hash(path: Path, h: hashlib._Hash, tick: Callable[[], None]) -> Iterator[None]:
    with path.open("rb") as f:
        while buf := f.read(8 * 1024 * 1024):
            h.update(buf)
            tick()
            yield
