import itertools

import pytest

from auditable_crud.state import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    Status,
    WorkflowError,
    ensure_not_requester,
    ensure_requester,
    ensure_transition,
)

HAPPY_PATH = [
    Status.DRAFT,
    Status.DRY_RUN_OK,
    Status.SUBMITTED,
    Status.APPROVED,
    Status.EXECUTING,
    Status.EXECUTED,
]


def test_happy_path_is_allowed() -> None:
    for current, new in itertools.pairwise(HAPPY_PATH):
        assert ensure_transition(current, new) is current


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (Status.DRAFT, Status.SUBMITTED),  # cannot skip the dry run
        (Status.DRY_RUN_OK, Status.APPROVED),  # cannot skip submission
        (Status.SUBMITTED, Status.EXECUTING),  # cannot skip approval
        (Status.EXECUTING, Status.CANCELLED),  # cannot cancel mid-flight
        (Status.EXECUTED, Status.EXECUTING),  # cannot run twice
    ],
)
def test_shortcuts_are_rejected(current: Status, new: Status) -> None:
    with pytest.raises(WorkflowError):
        ensure_transition(current, new)


def test_terminal_states_have_no_exit_and_every_state_is_reachable() -> None:
    assert {Status.EXECUTED, Status.FAILED, Status.REJECTED, Status.CANCELLED} == TERMINAL_STATUSES
    reachable = {Status.DRAFT} | {t for targets in TRANSITIONS.values() for t in targets}
    assert reachable == set(Status)


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(WorkflowError, match="Unknown"):
        ensure_transition("BOGUS", Status.DRAFT)


def test_separation_of_duties_is_case_insensitive() -> None:
    with pytest.raises(WorkflowError, match="cannot approve"):
        ensure_not_requester("jdoe", "JDOE ", "approve")
    ensure_not_requester("jdoe", "admin", "approve")
    with pytest.raises(WorkflowError, match="Only jdoe"):
        ensure_requester("jdoe", "admin", "submit")
