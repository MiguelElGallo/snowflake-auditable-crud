"""Change-request lifecycle and separation-of-duties rules.

``DRAFT -> DRY_RUN_OK -> SUBMITTED -> APPROVED -> EXECUTING -> EXECUTED | FAILED``

* A dry run may be repeated (``DRAFT|DRY_RUN_OK -> DRY_RUN_OK``).
* An admin may reject a submitted request, or send an approved one back to
  ``SUBMITTED`` when the source changed after approval.
* The requester may cancel any request that has not started executing.
"""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    """States of a change request (stored verbatim in CHANGE_REQUESTS.STATUS)."""

    DRAFT = "DRAFT"
    DRY_RUN_OK = "DRY_RUN_OK"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.DRAFT: frozenset({Status.DRY_RUN_OK, Status.CANCELLED}),
    Status.DRY_RUN_OK: frozenset({Status.DRY_RUN_OK, Status.SUBMITTED, Status.CANCELLED}),
    Status.SUBMITTED: frozenset({Status.APPROVED, Status.REJECTED, Status.CANCELLED}),
    # After a rolled-back execution the durable state is still APPROVED. Record
    # the failed attempt in its own transaction guarded by the approval revision.
    Status.APPROVED: frozenset(
        {Status.EXECUTING, Status.FAILED, Status.SUBMITTED, Status.CANCELLED}
    ),
    Status.EXECUTING: frozenset({Status.EXECUTED, Status.FAILED}),
    Status.EXECUTED: frozenset(),
    Status.FAILED: frozenset(),
    Status.REJECTED: frozenset(),
    Status.CANCELLED: frozenset(),
}

TERMINAL_STATUSES = frozenset(s for s, targets in TRANSITIONS.items() if not targets)
EDITABLE_STATUSES = frozenset({Status.DRAFT, Status.DRY_RUN_OK})


class WorkflowError(ValueError):
    """Raised when an action is not permitted in the request's current state."""


def ensure_transition(current: str, new: Status) -> Status:
    """Validate ``current -> new`` and return the parsed current status."""
    try:
        status = Status(current)
    except ValueError as error:
        raise WorkflowError(f"Unknown status {current!r}") from error
    if new not in TRANSITIONS[status]:
        raise WorkflowError(f"A request in status {status} cannot move to {new}.")
    return status


def same_user(a: str, b: str) -> bool:
    """Compare Snowflake user names (case-insensitive, like unquoted identifiers)."""
    return a.strip().upper() == b.strip().upper()


def ensure_not_requester(requested_by: str, actor: str, action: str) -> None:
    """Enforce separation of duties: requesters never review or run their own change."""
    if same_user(requested_by, actor):
        raise WorkflowError(f"{actor} requested this change and cannot {action} it.")


def ensure_requester(requested_by: str, actor: str, action: str) -> None:
    """Only the requester may edit, dry-run, submit or cancel their request."""
    if not same_user(requested_by, actor):
        raise WorkflowError(f"Only {requested_by} can {action} this request.")
