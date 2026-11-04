"""Prometheus text exposition."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llmhub.budget_manager import limits
from llmhub.persistence import state as cstate

if TYPE_CHECKING:
    from llmhub.api_gateway.app import Runtime


def _esc(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._typed: set[str] = set()

    def add(self, name: str, value: float, **labels: object) -> None:
        if name not in self._typed:
            self._typed.add(name)
            self.lines.append(f"# TYPE {name} gauge")
        lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        self.lines.append(f"{name}{{{lab}}} {value:g}" if lab else f"{name} {value:g}")


def render_metrics(rt: Runtime) -> str:
    out = _Out()
    conn, now = rt.conn, rt.clock.now()
    for r in conn.execute("SELECT status, route, count(*) AS n FROM jobs GROUP BY status, route"):
        out.add("llmhub_jobs", r["n"], status=r["status"], route=r["route"])
    for r in conn.execute(
        "SELECT pool, priority, count(*) AS n FROM jobs WHERE status='queued' AND pool IS NOT NULL"
        " GROUP BY pool, priority"
    ):
        out.add("llmhub_queue_depth", r["n"], pool=r["pool"], priority=r["priority"])
    local_q = conn.execute(
        "SELECT count(*) FROM jobs WHERE pool IS NULL AND status='queued'"
    ).fetchone()[0]
    out.add("llmhub_local_queue_depth", local_q)
    for r in conn.execute("SELECT pool, state, count(*) AS n FROM instances GROUP BY pool, state"):
        out.add("llmhub_instances", r["n"], pool=r["pool"], state=r["state"])
    w = limits.windows(now, rt.cfg.budget.timezone)
    out.add(
        "llmhub_budget_committed_usd",
        limits.committed(conn, w.month_start, w.month_end, now),
        window="month",
    )
    out.add(
        "llmhub_budget_committed_usd",
        limits.committed(conn, w.day_start, w.day_end, now),
        window="day",
    )
    out.add("llmhub_budget_stage", int(limits.stage(conn, now, rt.cfg)))
    out.add("llmhub_halted", 1 if cstate.is_halted(conn) else 0)
    for name, st in rt.worker.stats.items():
        out.add("llmhub_local_ewma_tps", st.pp.value, backend=name, kind="pp")
        out.add("llmhub_local_ewma_tps", st.tg.value, backend=name, kind="tg")
        out.add("llmhub_local_available", 1 if st.available else 0, backend=name)
    for (path, code), n in sorted(rt.http_counts.items()):
        out.add("llmhub_http_requests_total", n, path=path, code=code)
    out.add("llmhub_loop_lag_s", rt.lag.take())
    out.add("llmhub_waiters", rt.waiter.count)
    return "\n".join(out.lines) + "\n"
