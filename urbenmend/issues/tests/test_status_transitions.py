"""T5.1 - Issue lifecycle transition validation (BR-16/C-7, DM-Q8)."""

from __future__ import annotations

import pytest

from urbenmend.api.exceptions import Conflict, UnprocessableEntity
from urbenmend.issues.models import IssueStatus
from urbenmend.issues.services import (
    ISSUE_STATUS_TRANSITIONS,
    REOPEN_ACTION,
    REOPENABLE_ISSUE_STATUSES,
    validate_issue_transition,
)


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (IssueStatus.SUBMITTED, IssueStatus.TRIAGED),
        (IssueStatus.TRIAGED, IssueStatus.ACKNOWLEDGED),
        (IssueStatus.TRIAGED, IssueStatus.REJECTED),
        (IssueStatus.TRIAGED, IssueStatus.DUPLICATE),
        (IssueStatus.TRIAGED, IssueStatus.INSUFFICIENT_INFO),
        (IssueStatus.ACKNOWLEDGED, IssueStatus.IN_PROGRESS),
        (IssueStatus.IN_PROGRESS, IssueStatus.RESOLVED),
        (IssueStatus.RESOLVED, IssueStatus.CLOSED),
    ],
)
def test_documented_status_edges_are_accepted(from_status: str, to_status: str) -> None:
    reason = (
        "Required branch explanation."
        if to_status
        in {
            IssueStatus.REJECTED,
            IssueStatus.DUPLICATE,
            IssueStatus.INSUFFICIENT_INFO,
        }
        else None
    )

    plan = validate_issue_transition(
        from_status=from_status,
        to_status=to_status,
        reason=reason,
    )

    assert plan.from_status == from_status
    assert plan.to_status == to_status
    assert plan.reason == reason
    assert plan.creates_new_issue is False


def test_transition_table_covers_every_persisted_issue_status() -> None:
    assert set(ISSUE_STATUS_TRANSITIONS) == set(IssueStatus.values)


@pytest.mark.parametrize("status", IssueStatus.values)
def test_same_status_transition_is_rejected(status: str) -> None:
    with pytest.raises(Conflict) as caught:
        validate_issue_transition(from_status=status, to_status=status)

    assert caught.value.status_code == 409
    assert getattr(caught.value.detail, "code", None) == "INVALID_TRANSITION"


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (IssueStatus.SUBMITTED, IssueStatus.ACKNOWLEDGED),
        (IssueStatus.TRIAGED, IssueStatus.IN_PROGRESS),
        (IssueStatus.ACKNOWLEDGED, IssueStatus.RESOLVED),
        (IssueStatus.IN_PROGRESS, IssueStatus.CLOSED),
        (IssueStatus.CLOSED, IssueStatus.TRIAGED),
        (IssueStatus.REJECTED, IssueStatus.TRIAGED),
        (IssueStatus.DUPLICATE, IssueStatus.TRIAGED),
        (IssueStatus.INSUFFICIENT_INFO, IssueStatus.TRIAGED),
        (IssueStatus.HIDDEN, IssueStatus.TRIAGED),
        (IssueStatus.REMOVED, IssueStatus.TRIAGED),
        ("unknown", IssueStatus.TRIAGED),
        (IssueStatus.TRIAGED, "unknown"),
    ],
)
def test_skips_terminal_returns_moderation_and_unknown_edges_are_rejected(
    from_status: str,
    to_status: str,
) -> None:
    with pytest.raises(Conflict, match="cannot transition"):
        validate_issue_transition(from_status=from_status, to_status=to_status)


@pytest.mark.parametrize(
    "to_status",
    [IssueStatus.REJECTED, IssueStatus.DUPLICATE, IssueStatus.INSUFFICIENT_INFO],
)
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_terminal_triage_branches_require_a_non_blank_reason(
    to_status: str,
    reason: str | None,
) -> None:
    with pytest.raises(UnprocessableEntity) as caught:
        validate_issue_transition(
            from_status=IssueStatus.TRIAGED,
            to_status=to_status,
            reason=reason,
        )

    assert caught.value.status_code == 422


def test_reason_is_trimmed_before_it_reaches_the_mutation_service() -> None:
    plan = validate_issue_transition(
        from_status=IssueStatus.TRIAGED,
        to_status=IssueStatus.REJECTED,
        reason="  Outside municipal responsibility.  ",
    )

    assert plan.reason == "Outside municipal responsibility."


@pytest.mark.parametrize("from_status", REOPENABLE_ISSUE_STATUSES)
def test_reopen_is_a_create_new_issue_action(from_status: str) -> None:
    plan = validate_issue_transition(
        from_status=from_status,
        to_status=REOPEN_ACTION,
        reason="The problem has recurred.",
    )

    assert REOPEN_ACTION not in IssueStatus.values
    assert plan.to_status == REOPEN_ACTION
    assert plan.creates_new_issue is True
    assert plan.reason == "The problem has recurred."


@pytest.mark.parametrize("reason", [None, "", "\t"])
def test_reopen_requires_a_non_blank_reason(reason: str | None) -> None:
    with pytest.raises(UnprocessableEntity):
        validate_issue_transition(
            from_status=IssueStatus.RESOLVED,
            to_status=REOPEN_ACTION,
            reason=reason,
        )


@pytest.mark.parametrize(
    "from_status",
    [status for status in IssueStatus.values if status not in REOPENABLE_ISSUE_STATUSES],
)
def test_reopen_is_rejected_from_every_non_terminal_workflow_state(from_status: str) -> None:
    with pytest.raises(Conflict) as caught:
        validate_issue_transition(
            from_status=from_status,
            to_status=REOPEN_ACTION,
            reason="The problem has recurred.",
        )

    assert getattr(caught.value.detail, "code", None) == "INVALID_TRANSITION"
