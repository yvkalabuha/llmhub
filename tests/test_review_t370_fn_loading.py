"""Review T-370 cycle 2: LOADING_MODEL progress keys and early-E7 money checks."""

from __future__ import annotations

from pathlib import Path

from harness import make_job
from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
from test_w_provisioner_loading import make_h, reasons


def _load_keys(h: object) -> int:
    conn = h.conn  # type: ignore[attr-defined]
    # keys of instances that left LOADING_MODEL (fix round 1: after E7 the controller already
    # loads a NEW instance whose own progress key is legitimate)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM controller_state WHERE key LIKE 'load_%' AND"
            " substr(key, instr(key, ':') + 1) NOT IN"
            " (SELECT id FROM instances WHERE state='LOADING_MODEL')"
        ).fetchone()[0]
    )


async def test_progress_keys_cleared_after_timeout_e7(tmp_path: Path) -> None:
    knobs = MockInferenceKnobs(hang_load=True, load_eta_s=5000, load_sources_left=1)
    h = make_h(tmp_path, knobs=knobs)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 1500)
    assert "loading_model_timeout" in reasons(h)
    assert _load_keys(h) == 0


async def test_slow_but_fits_budget_is_not_killed(tmp_path: Path) -> None:
    """Last source, ETA inside the LOADING_MODEL budget (minus 60 s llama load) -> no early E7."""
    knobs = MockInferenceKnobs(hang_load=True, load_eta_s=1000, load_sources_left=0)
    h = make_h(tmp_path, knobs=knobs)
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 200)
    assert "weights_too_slow" not in reasons(h) and h.state_of() == "LOADING_MODEL"


async def test_llama_load_phase_never_counts_as_slow(tmp_path: Path) -> None:
    """Weights done, llama-server loading: `phase` is not a download phase -> no early E7."""
    knobs = MockInferenceKnobs(hang_load=True, load_eta_s=99999, load_sources_left=0)
    h = make_h(tmp_path, knobs=knobs)
    inner = h.inference.load_status

    async def llama_load(endpoint: str):  # type: ignore[no-untyped-def]
        st = await inner(endpoint)
        if st is None:
            return None
        from dataclasses import replace

        return replace(st, phase="llama_load")

    h.inference.load_status = llama_load  # type: ignore[method-assign]
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 600)
    assert "weights_too_slow" not in reasons(h)
