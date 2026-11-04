"""Generate simulator-parity fixtures (T-368 F9). Run by hand, never by the tests:

    uv run python tests/fixtures/sim/generate_sim_fixtures.py \
        /var/www/projectVoiceBot/tools/bench/T-367-sim/sim.py

The research simulator is loaded BY PATH only here (llmhub never imports projectVoiceBot at
runtime or test time). For each trace (S1-S6 with the customer's Params; RF2/RF4/RF6 with the
"start 9 min, 0.22 $/h" configuration), seeds 0-2 and the strategies `idle 5m` and
`batch 5/30m drain`, one JSON file stores: header (sha256 of sim.py, command, date), params,
strategy, requests and the reference result of `simulate()` (launches, total $).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent
SEEDS = (0, 1, 2)


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("t367_sim", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["t367_sim"] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def _num(x: float) -> float | None:
    return None if math.isinf(x) else x


def main(sim_path: str) -> None:
    path = Path(sim_path)
    sim = _load(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    customer = sim.Params.customer()
    rf_params = sim.Params(price_per_h=0.22, startup_s=540, gen_tps=61, prefill_tps=2000)
    strategies = {
        "idle5m": sim.Strategy("idle 5m", idle_s=300.0),
        "batch_drain": sim.Strategy(
            "batch 5/30m drain", idle_s=0, min_batch=5, max_batch_wait_s=1800.0,
            max_queue_wait_s=2700.0,
        ),
    }
    traces = [(sc, customer) for sc in sim.SCENARIOS]
    traces += [(sim.rare_flow(rate), rf_params) for rate in (2.0, 4.0, 6.0)]
    header = {
        "sim_sha256": digest,
        "sim_path": str(path),
        "command": "uv run python tests/fixtures/sim/generate_sim_fixtures.py " + str(path),
        "generated": dt.date.today().isoformat(),
    }
    count = 0
    for sc, params in traces:
        for seed in SEEDS:
            reqs = sc.gen(random.Random(1000 + seed))  # same seeding as sim.run_many
            for key, strat in strategies.items():
                res = sim.simulate(reqs, strat, params, duration_s=sc.duration_s, seed=seed)
                doc = {
                    "header": header,
                    "trace": sc.key,
                    "seed": seed,
                    "duration_s": sc.duration_s,
                    "params": {
                        "price_per_h": params.price_per_h, "startup_s": params.startup_s,
                        "gen_tps": params.gen_tps, "prefill_tps": params.prefill_tps,
                    },
                    "strategy": {
                        "key": key, "name": strat.name, "idle_s": strat.idle_s,
                        "min_batch": strat.min_batch, "max_batch_wait_s": strat.max_batch_wait_s,
                        "max_queue_wait_s": _num(strat.max_queue_wait_s),
                        "urgent_bypass": strat.urgent_bypass,
                    },
                    "requests": [
                        {"t": r.t, "prio": r.prio, "in": r.in_tokens, "out": r.out_tokens,
                         "max_wait_s": r.max_wait_s, "agent": r.agent}
                        for r in sorted(reqs, key=lambda r: (r.t, r.rid))
                    ],
                    "expected": {"launches": res.launches, "total_usd": res.total},
                }
                name = f"{sc.key}_s{seed}_{key}.json"
                (OUT / name).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
                count += 1
    print(f"wrote {count} fixtures to {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/var/www/projectVoiceBot/tools/bench/T-367-sim/sim.py")
