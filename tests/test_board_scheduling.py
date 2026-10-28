import pytest

from board_core import agentLanes
from board_scheduling import availableLane


@pytest.mark.parametrize("role", ["writer", "reviewer"])
def test_main_runs_first_and_children_only_fill_the_backlog(role):
    lanes = agentLanes(role, {"maxParallel": 3})
    busy = set()
    for expected in (role, f"{role}-sub1", f"{role}-sub2"):
        lane = availableLane(lanes, busy)
        assert lane == expected
        busy.add(lane)
    assert availableLane(lanes, busy) is None
    busy.remove(role)
    assert availableLane(lanes, busy) == role


@pytest.mark.parametrize("role", ["writer", "reviewer"])
def test_free_main_is_used_even_when_a_retry_previously_used_a_child(role):
    lanes = agentLanes(role, {"maxParallel": 3})
    assert availableLane(lanes, set(), preferred=f"{role}-sub2") == role
    assert availableLane(lanes, {role}, preferred=f"{role}-sub2") == f"{role}-sub2"


def test_no_capacity_or_unknown_preference_does_not_invent_a_lane():
    assert availableLane([], set()) is None
    assert availableLane(["writer"], set(), "writer-sub9") == "writer"
    assert availableLane(["writer"], {"writer"}) is None
