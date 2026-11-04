"""C1: TRANSITIONS equals the plan section 5 table, edge by edge."""

import pytest

from llmhub.lifecycle_manager.states import (
    BILLABLE,
    LIVE_IN_INDEX,
    TRANSITIONS,
    IllegalTransition,
    State,
    check_edge,
)

S = State
LIVE6 = [S.READY, S.IDLE, S.BUSY, S.PROVISIONING, S.BOOTING, S.LOADING_MODEL]

EXPECTED = {
    (S.STOPPED, S.PROVISIONING, "E1"),
    (S.PROVISIONING, S.BOOTING, "E2"),
    (S.PROVISIONING, S.ERROR, "E3"),
    (S.BOOTING, S.LOADING_MODEL, "E4"),
    (S.BOOTING, S.ERROR, "E5"),
    (S.LOADING_MODEL, S.READY, "E6"),
    (S.LOADING_MODEL, S.ERROR, "E7"),
    (S.READY, S.BUSY, "E8"),
    (S.READY, S.IDLE, "E9"),
    (S.READY, S.ERROR, "E10"),
    (S.BUSY, S.BUSY, "E11"),
    (S.BUSY, S.IDLE, "E12"),
    (S.IDLE, S.BUSY, "E13"),
    (S.IDLE, S.STOPPING, "E14"),
    *((s, S.STOPPING, "E15") for s in LIVE6),
    (S.BUSY, S.STOPPING, "E16"),
    (S.BUSY, S.ERROR, "E17"),
    (S.IDLE, S.ERROR, "E17"),
    (S.ERROR, S.STOPPING, "E18"),
    (S.STOPPING, S.PENDING_DELETE, "E19"),
    (S.PENDING_DELETE, S.STOPPED, "E20"),
    (S.PENDING_DELETE, S.LEAKED, "E21"),
    (S.LEAKED, S.STOPPED, "E22"),
}


def test_table_matches_plan() -> None:
    assert {(e.src, e.dst, e.trigger) for e in TRANSITIONS} == EXPECTED
    assert len(TRANSITIONS) == 28
    assert {e.trigger for e in TRANSITIONS} == {f"E{i}" for i in range(1, 23)}


def test_every_listed_edge_passes_check() -> None:
    for src, dst, trig in EXPECTED:
        assert check_edge(src, dst, trig).trigger == trig


@pytest.mark.parametrize(
    ("src", "dst", "trig"),
    [
        (S.STOPPED, S.BUSY, "E8"),
        (S.PENDING_DELETE, S.READY, "E6"),
        (S.STOPPING, S.ERROR, "E3"),  # section 14: no STOPPING -> ERROR
        (S.STOPPING, S.STOPPED, "E20"),  # must pass PENDING_DELETE
        (S.READY, S.STOPPED, "E20"),
        (S.LEAKED, S.READY, "E22"),
        (S.BOOTING, S.READY, "E6"),
        (S.READY, S.BUSY, "E13"),  # right states, wrong edge id
        (S.ERROR, S.IDLE, "E9"),
    ],
)
def test_forbidden_edges_raise(src: State, dst: State, trig: str) -> None:
    with pytest.raises(IllegalTransition):
        check_edge(src, dst, trig)


def test_index_and_billable_sets() -> None:
    assert S.STOPPING in LIVE_IN_INDEX and S.ERROR in LIVE_IN_INDEX
    assert not {S.STOPPED, S.PENDING_DELETE, S.LEAKED} & LIVE_IN_INDEX
    assert S.STOPPED not in BILLABLE
    assert {S.PENDING_DELETE, S.LEAKED} <= BILLABLE
