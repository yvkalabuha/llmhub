"""T-369 review cycle 3: loop supervision exit code, SecretMaskFilter robustness, harness
TrackingExecutor. The R3 defects are fixed (round 2); the xfail markers were removed."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import harness
from int_fixtures import ENV, settle
from llmhub.api_gateway.logging import SecretMaskFilter
from llmhub.clock import FakeClock
from llmhub.configuration.loader import load_config
from llmhub.server import build_service, serve

FIXTURE = Path(__file__).parent / "fixtures" / "config_api.yaml"


# --- supervision: serve() exit code ---------------------------------------------------------


def _run_serve(tmp: Path, crash: bool) -> tuple[int | None, BaseException | None]:
    """serve() in a worker thread (no signal handlers there), port 0, FakeClock."""
    out: list[Any] = [None, None]

    def main() -> None:
        async def go() -> int:
            cfg = load_config(FIXTURE)
            cfg.data_dir = tmp
            svc = build_service(cfg, ENV, FakeClock())
            if crash:

                async def boom(_stop: asyncio.Event) -> None:
                    await asyncio.sleep(0.05)
                    raise RuntimeError("controller died")

                svc.rt.controller.run_forever = boom  # type: ignore[method-assign]
            return await serve(svc, host="127.0.0.1", port=0)

        try:
            out[0] = asyncio.run(go())
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            out[1] = exc

    t = threading.Thread(target=main, daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "serve did not stop after a fatal loop"
    return out[0], out[1]


def test_dead_controller_stops_serve_with_exit_code_1(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.CRITICAL, logger="llmhub.api.app"):
        code, exc = _run_serve(tmp_path, crash=True)
    assert exc is None and code == 1


# --- SecretMaskFilter -------------------------------------------------------------------------


def _record(msg: object, args: Any, exc_info: Any = None) -> logging.LogRecord:
    return logging.LogRecord("llmhub.t", logging.ERROR, __file__, 1, msg, args, exc_info)


def test_mask_filter_non_str_msg_and_exc_info() -> None:
    token = "ghp_" + "c" * 36
    try:
        raise ValueError(f"upstream said {token}")
    except ValueError as err:
        rec = _record(err, None, (type(err), err, err.__traceback__))
    assert SecretMaskFilter().filter(rec) is True
    text = logging.Formatter().format(rec)
    assert token not in text and "ValueError" in text and "Traceback" in text
    rec2 = _record({"api_key": "sekrit-value"}, None)
    assert SecretMaskFilter().filter(rec2) is True
    assert "sekrit-value" not in logging.Formatter().format(rec2)


def test_mask_filter_never_raises_on_bad_format() -> None:
    rec = _record("%(missing)s", ({"other": 1},))
    assert SecretMaskFilter().filter(rec) is True


# --- harness TrackingExecutor -------------------------------------------------------------------


async def test_settle_waits_until_to_thread_result_is_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.install_tracking_executor()
    orig = harness.TrackingExecutor._done  # pyright: ignore[reportPrivateUsage]

    def slow_done(self: harness.TrackingExecutor, fut: Any) -> None:
        orig(self, fut)
        time.sleep(0.05)  # widen the window between our callback and asyncio's

    monkeypatch.setattr(harness.TrackingExecutor, "_done", slow_done)
    def work() -> int:
        time.sleep(0.02)  # still running when the executor registers its callbacks
        return 1

    fut = asyncio.ensure_future(asyncio.to_thread(work))
    await asyncio.sleep(0)
    await settle()
    done = fut.done()
    await fut
    assert done
