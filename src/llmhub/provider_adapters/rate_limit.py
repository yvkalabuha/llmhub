"""Provider REST rate limit: an in-process limiter plus its shared state in the DB (T-372 L2b).

Live finding (2026-11-05): RunPod REST answers `429 {"error": "rate limit exceeded for the hour
window"}` after about an hour of polling every 60 s (guard + probe); GraphQL kept working.

`RestLimiter` sits in front of the HTTP client of an adapter (RunPod: `RunpodApi`):
- a 429 starts a HOLD until `now + Retry-After` (the header, else `hold_s`, default 15 min);
- during the hold `POST /pods` (create) is refused without an HTTP call; `DELETE` always goes
  out (money first); other REST calls go out only in a short burst (`burst_s`, 5 s) once per
  `trickle_s` (60 s), so a teardown still sees the pod list (`list_owned` = 3 GETs); a 2xx does not end the hold (live: one 200 inside the window was
  followed by another 429), only time does;
- `POST /pods` is counted: at most `post_budget_per_hour` in any 3600 s, the next is refused
  without an HTTP call until a slot frees.
A refusal is a definite one (nothing was sent).

The state is shared through `controller_state` key `rest_rate:<provider>` (JSON), so the
controller, the reaper and the operator CLI of one data dir see one hold and one POST count:
`GuardedAdapter` absorbs the DB state before each call and writes it back after. The hold is
merged by `hold_changed_at` (the newest decision wins; on a tie the later end), the POST
times by union.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from typing import Any

from llmhub.persistence.db import Busy, tx

HOUR_S = 3600.0
DEFAULT_HOLD_S = 900.0
DEFAULT_POST_BUDGET = 60
DEFAULT_TRICKLE_S = 60.0
DEFAULT_BURST_S = 5.0
KEY_PREFIX = "rest_rate:"
RATE_LIMITED = "rate_limited"
POST_BUDGET = "post_budget"
CREATE_PATH = "/pods"


class RestLimiter:
    def __init__(
        self,
        now: Callable[[], float],
        *,
        hold_s: float = DEFAULT_HOLD_S,
        post_budget_per_hour: int = DEFAULT_POST_BUDGET,
        trickle_s: float = DEFAULT_TRICKLE_S,
        burst_s: float = DEFAULT_BURST_S,
        create_path: str = CREATE_PATH,
    ) -> None:
        self._now = now
        self.hold_s = float(hold_s)
        self.post_budget_per_hour = int(post_budget_per_hour)
        self.trickle_s = float(trickle_s)
        self.burst_s = float(burst_s)
        self.create_path = create_path
        self.hold_until = 0.0
        self.hold_cause: str | None = None
        self.hold_changed_at = 0.0
        self.posts: list[float] = []
        self._last_trickle = -math.inf

    def __repr__(self) -> str:
        return f"RestLimiter(hold_until={self.hold_until:.0f}, posts={len(self.posts)})"

    # --- queries ---------------------------------------------------------------
    def held(self, now: float | None = None) -> bool:
        return (self._now() if now is None else now) < self.hold_until

    def _prune(self, now: float) -> None:
        self.posts = sorted(t for t in self.posts if now - t < HOUR_S)

    def posts_last_hour(self, now: float | None = None) -> int:
        n = self._now() if now is None else now
        self._prune(n)
        return len(self.posts)

    def wait_s(self, code: str, now: float | None = None) -> float:
        """Seconds until a refused call of kind `code` may go out again."""
        n = self._now() if now is None else now
        if code == POST_BUDGET:
            self._prune(n)
            if len(self.posts) < self.post_budget_per_hour:
                return 0.0
            return max(0.0, self.posts[len(self.posts) - self.post_budget_per_hour] + HOUR_S - n)
        return max(0.0, self.hold_until - n)

    # --- the gate ------------------------------------------------------------------
    def admit(self, method: str, path: str) -> str | None:
        """None = send the request; else the refusal code (nothing is sent)."""
        now = self._now()
        if method == "POST" and path == self.create_path:
            if self.held(now):
                return RATE_LIMITED
            self._prune(now)
            if len(self.posts) >= self.post_budget_per_hour:
                return POST_BUDGET
            self.posts.append(now)
            return None
        if method == "DELETE" or not self.held(now):
            return None
        since = now - self._last_trickle
        if since < self.burst_s:
            return None  # inside the open burst (one logical read = several GETs)
        if since < self.trickle_s:
            return RATE_LIMITED
        self._last_trickle = now
        return None

    def on_response(self, method: str, status: int, retry_after_s: float | None) -> float | None:
        """Record an answer. A 429 returns the effective hold in seconds."""
        now = self._now()
        if status == 429:
            wait = self.hold_s if retry_after_s is None else max(retry_after_s, 1.0)
            until = now + wait
            if until > self.hold_until:
                self.hold_until = until
                self.hold_cause = "http_429"
                self.hold_changed_at = now
            self._last_trickle = now - self.burst_s  # burst closed; next try in `trickle_s`
            return self.hold_until - now
        # A 2xx during the hold does NOT end it: live 2026-11-05 01:00 one REST call got 200
        # inside the window and the next GET got 429 again. The hold ends by time only.
        return None

    # --- shared state ----------------------------------------------------------------
    def export(self) -> dict[str, Any]:
        now = self._now()
        self._prune(now)
        return {"hold_until": self.hold_until, "hold_cause": self.hold_cause,
                "hold_changed_at": self.hold_changed_at, "posts": list(self.posts),
                "post_budget_per_hour": self.post_budget_per_hour}  # fmt: skip

    def absorb(self, state: dict[str, Any] | None) -> None:
        if not state:
            return
        try:
            changed = float(state.get("hold_changed_at", 0.0))
            until = float(state.get("hold_until", 0.0))
            cause = state.get("hold_cause")
            posts = [float(t) for t in state.get("posts", [])]
        except (TypeError, ValueError):
            return  # unreadable: keep our own view
        if changed > self.hold_changed_at or (changed == self.hold_changed_at
                                               and until > self.hold_until):  # fmt: skip
            self.hold_until = until
            self.hold_cause = cause if isinstance(cause, str) else None
            self.hold_changed_at = changed
        now = self._now()
        self.posts = sorted({t for t in (*self.posts, *posts) if now - t < HOUR_S})


# ------------------------------------------------------------------ DB state
def _key(provider: str) -> str:
    return f"{KEY_PREFIX}{provider}"


def load_state(conn: sqlite3.Connection, provider: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT value FROM controller_state WHERE key=?",
                       (_key(provider),)).fetchone()  # fmt: skip
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def sync(conn: sqlite3.Connection, provider: str, limiter: RestLimiter, now: float) -> None:
    """Merge the DB state into `limiter` and write the merged state back (best effort)."""
    try:
        with tx(conn):
            stored = load_state(conn, provider)
            limiter.absorb(stored)
            value = limiter.export()
            if stored == value or (stored is None and not value["posts"]
                                   and value["hold_until"] <= now):  # fmt: skip
                return  # nothing new: no write
            conn.execute(
                "INSERT INTO controller_state(key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (_key(provider), json.dumps(value, sort_keys=True), now),
            )
    except Busy:
        pass  # the next call merges again


def absorb_only(conn: sqlite3.Connection, provider: str, limiter: RestLimiter) -> None:
    limiter.absorb(load_state(conn, provider))


def set_hold(conn: sqlite3.Connection, provider: str, until: float, cause: str,
             now: float) -> None:  # fmt: skip
    """A hold for an adapter without its own limiter (a rate-limited answer it normalized)."""
    try:
        with tx(conn):
            state = load_state(conn, provider) or {}
            if float(state.get("hold_until", 0.0) or 0.0) >= until:
                return
            state.update(hold_until=until, hold_cause=cause, hold_changed_at=now)
            conn.execute(
                "INSERT INTO controller_state(key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (_key(provider), json.dumps(state, sort_keys=True), now),
            )
    except Busy:
        pass


def create_blocked(conn: sqlite3.Connection, provider: str, now: float
                   ) -> tuple[float, str] | None:  # fmt: skip
    """(until, cause) while a create would be refused by the shared state: a live hold or a
    spent POST budget; None otherwise."""
    state = load_state(conn, provider)
    if not state:
        return None
    try:
        until = float(state.get("hold_until", 0.0) or 0.0)
        posts = sorted(float(t) for t in state.get("posts", []) if now - float(t) < HOUR_S)
        budget = int(state.get("post_budget_per_hour", 0) or 0)
    except (TypeError, ValueError):
        return None
    if now < until:
        return until, RATE_LIMITED
    if budget > 0 and len(posts) >= budget:
        return posts[len(posts) - budget] + HOUR_S, POST_BUDGET
    return None


def status_view(conn: sqlite3.Connection, provider: str, now: float) -> dict[str, Any] | None:
    """For /admin/status: the hold and the POST count of the last hour."""
    state = load_state(conn, provider)
    if state is None:
        return None
    try:
        until = float(state.get("hold_until", 0.0) or 0.0)
        posts = [float(t) for t in state.get("posts", []) if now - float(t) < HOUR_S]
        budget = int(state.get("post_budget_per_hour", 0) or 0)
    except (TypeError, ValueError):
        return None
    held = now < until
    return {"held": held, "hold_until": until if held else None,
            "hold_cause": state.get("hold_cause") if held else None,
            "posts_last_hour": len(posts), "post_budget_per_hour": budget or None}  # fmt: skip


__all__ = [
    "DEFAULT_HOLD_S", "DEFAULT_POST_BUDGET", "POST_BUDGET", "RATE_LIMITED", "RestLimiter",
    "absorb_only", "create_blocked", "load_state", "set_hold", "status_view", "sync",
]  # fmt: skip
