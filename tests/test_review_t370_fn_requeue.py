"""Review T-370 cycle 2 (functions/failures/money): NetworkLost requeue loop and header wait.

Both defects (F1, F2) were fixed in fix round 1; the xfail markers are removed.
"""

from __future__ import annotations

from pathlib import Path

from harness import Harness, make_job
from llmhub.cloud_manager.inference import JobRunResult, JobRunSpec, NetworkLost
from test_g_http_inference import EP, Env, add_inst


async def test_long_nonstream_job_within_cap_is_not_network_lost(tmp_path: Path) -> None:
    e = Env(tmp_path)  # read_s 0.3 s, connect 1 s
    add_inst(e.conn)
    e.fake.delay_s = 2.0  # generation time: > read_s + connect_s, << job cap (~30 s)
    spec = JobRunSpec(job_id="j-1", model_alias="m1", tokens_in=100, max_tokens=10,
                      body=b'{"messages":[{"role":"user","content":"hi"}],"stream":false}')  # fmt: skip
    res = await e.backend.run(EP, spec)
    assert res.finish_reason != "error"


async def test_repeated_network_lost_with_live_front_is_bounded(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    runs: list[float] = []

    async def always_lost(endpoint: str, job: JobRunSpec) -> JobRunResult:
        runs.append(h.clock.now())
        await h.clock.sleep_until(h.clock.now() + 130)  # e.g. header wait read_s + connect_s
        raise NetworkLost("timeout")

    h.inference.run = always_lost  # type: ignore[method-assign]
    h.submit(make_job(0))
    await h.run_for(2 * 3600)
    job = h.jobs()[0]
    # desired: a few tries, then the job leaves the GPU (attempts exhausted -> local fallback)
    assert len(runs) <= 2 * h.cfg.retry.job_max_attempts, (len(runs), job["status"])
