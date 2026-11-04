"""GPU instance state machine: states and the exact edge table (plan section 5).

A `trigger` is the edge id `E1`..`E22` from the plan; several edges may share one id
(E15, E17). `transitions.reason` carries the human-readable detail.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class State(enum.StrEnum):
    STOPPED = "STOPPED"
    PROVISIONING = "PROVISIONING"
    BOOTING = "BOOTING"
    LOADING_MODEL = "LOADING_MODEL"
    READY = "READY"
    BUSY = "BUSY"
    IDLE = "IDLE"
    STOPPING = "STOPPING"
    PENDING_DELETE = "PENDING_DELETE"
    LEAKED = "LEAKED"
    ERROR = "ERROR"


LIVE_IN_INDEX = frozenset(State) - {State.STOPPED, State.PENDING_DELETE, State.LEAKED}
BILLABLE = frozenset(State) - {State.STOPPED}


class IllegalTransition(Exception):
    pass


@dataclass(frozen=True)
class Edge:
    src: State
    dst: State
    trigger: str


S = State
_LIVE_TO_STOPPING = (S.READY, S.IDLE, S.BUSY, S.PROVISIONING, S.BOOTING, S.LOADING_MODEL)

TRANSITIONS: frozenset[Edge] = frozenset(
    [
        Edge(S.STOPPED, S.PROVISIONING, "E1"),
        Edge(S.PROVISIONING, S.BOOTING, "E2"),
        Edge(S.PROVISIONING, S.ERROR, "E3"),
        Edge(S.BOOTING, S.LOADING_MODEL, "E4"),
        Edge(S.BOOTING, S.ERROR, "E5"),
        Edge(S.LOADING_MODEL, S.READY, "E6"),
        Edge(S.LOADING_MODEL, S.ERROR, "E7"),
        Edge(S.READY, S.BUSY, "E8"),
        Edge(S.READY, S.IDLE, "E9"),
        Edge(S.READY, S.ERROR, "E10"),
        Edge(S.BUSY, S.BUSY, "E11"),  # no transitions row is written
        Edge(S.BUSY, S.IDLE, "E12"),
        Edge(S.IDLE, S.BUSY, "E13"),
        Edge(S.IDLE, S.STOPPING, "E14"),
        *(Edge(src, S.STOPPING, "E15") for src in _LIVE_TO_STOPPING),
        Edge(S.BUSY, S.STOPPING, "E16"),
        Edge(S.BUSY, S.ERROR, "E17"),
        Edge(S.IDLE, S.ERROR, "E17"),
        Edge(S.ERROR, S.STOPPING, "E18"),
        Edge(S.STOPPING, S.PENDING_DELETE, "E19"),
        Edge(S.PENDING_DELETE, S.STOPPED, "E20"),
        Edge(S.PENDING_DELETE, S.LEAKED, "E21"),
        Edge(S.LEAKED, S.STOPPED, "E22"),
    ]
)


def check_edge(src: State, dst: State, trigger: str) -> Edge:
    edge = Edge(src, dst, trigger)
    if edge not in TRANSITIONS:
        raise IllegalTransition(f"{src} -> {dst} via {trigger}")
    return edge
