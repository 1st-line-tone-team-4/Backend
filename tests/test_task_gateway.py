from dataclasses import replace

import pytest

from app.core import ApiError
from app.ports import QuestionTaskState, TaskGateway


STATE = QuestionTaskState(
    task_id="task",
    user_id="owner",
    unit_id="unit",
    concept_version_ids=frozenset({"v1", "v2"}),
    task_type="solve",
    active_revision=True,
    target_count=5,
    completed_count=3,
)


class Reader(TaskGateway):
    def __init__(self, state):
        self.state = state
        self.reads = []

    def read_question_task(self, session, user_id, task_id):
        self.reads.append((session, user_id, task_id))
        return self.state


def check(reader, count=2):
    return reader.validate_question_task(
        "same-transaction", "owner", "task", "unit", ["v2", "v1"], count=count
    )


def test_task_reader_receives_transaction_and_checks_remaining_without_progress_writes():
    reader = Reader(STATE)
    check(reader)
    assert reader.reads == [("same-transaction", "owner", "task")]
    assert reader.state.completed_count == 3
    with pytest.raises(ApiError) as error:
        check(reader, count=3)
    assert error.value.code == "TASK_TARGET_EXCEEDED"


@pytest.mark.parametrize(
    "state,code",
    [
        (None, "NOT_FOUND"),
        (replace(STATE, user_id="other"), "NOT_FOUND"),
        (replace(STATE, task_id="other"), "NOT_FOUND"),
        (replace(STATE, unit_id="other"), "RELATION_MISMATCH"),
        (replace(STATE, concept_version_ids=frozenset({"v1"})), "RELATION_MISMATCH"),
        (replace(STATE, task_type="teach_back"), "INVALID_TASK_TYPE"),
        (replace(STATE, task_type="review"), "INVALID_TASK_TYPE"),
        (replace(STATE, active_revision=False), "STALE_TASK"),
        (replace(STATE, active_revision="true"), "TASK_INTEGRATION_UNAVAILABLE"),
        (replace(STATE, completed_count=5), "TASK_TARGET_EXCEEDED"),
        (replace(STATE, completed_count=6), "TASK_TARGET_EXCEEDED"),
        (replace(STATE, completed_count=-1), "TASK_INTEGRATION_UNAVAILABLE"),
        (replace(STATE, completed_count=True), "TASK_INTEGRATION_UNAVAILABLE"),
        (replace(STATE, target_count=21), "TASK_INTEGRATION_UNAVAILABLE"),
        ({"target_count": 5}, "TASK_INTEGRATION_UNAVAILABLE"),
    ],
)
def test_task_boundary_fails_closed_for_invalid_or_unrelated_state(state, code):
    with pytest.raises(ApiError) as error:
        check(Reader(state))
    assert error.value.code == code


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_task_boundary_rejects_invalid_counts(count):
    with pytest.raises(ApiError) as error:
        check(Reader(STATE), count)
    assert error.value.code == "TASK_TARGET_EXCEEDED"
