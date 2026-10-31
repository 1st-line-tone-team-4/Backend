from dataclasses import dataclass

from .core import ApiError


@dataclass(frozen=True)
class QuestionTaskState:
    """A-owned task facts read in the caller's transaction, without progress writes."""

    task_id: str
    user_id: str
    unit_id: str
    concept_version_ids: frozenset[str]
    task_type: str
    active_revision: bool
    target_count: int
    completed_count: int


class TaskGateway:
    """A integration boundary. An unconfigured adapter denies task usage and deletion."""

    def read_question_task(self, session, user_id, task_id):
        """A implements this reader and returns QuestionTaskState or None.

        Use the supplied transaction and lock the owning plan and task while
        reading active revision and authoritative graded progress. A plan/progress
        writers must use the same locks and lock order. Do not
        reserve or increment completion here: generating questions is not solving.
        """
        raise ApiError(503, "TASK_INTEGRATION_UNAVAILABLE", "Task integration is unavailable.")

    def validate_question_task(self, session, user_id, task_id, unit_id, concept_version_ids, *, count):
        state = self.read_question_task(session, user_id, task_id)
        if state is None:
            raise ApiError(404, "NOT_FOUND", "Task not found.")
        if not isinstance(state, QuestionTaskState):
            raise ApiError(503, "TASK_INTEGRATION_UNAVAILABLE", "Task integration is unavailable.")
        if state.task_id != task_id or state.user_id != user_id:
            raise ApiError(404, "NOT_FOUND", "Task not found.")
        if state.unit_id != unit_id or state.concept_version_ids != frozenset(concept_version_ids):
            raise ApiError(422, "RELATION_MISMATCH", "Task versions do not match.")
        if state.task_type != "solve":
            raise ApiError(422, "INVALID_TASK_TYPE", "Question generation requires a solve task.")
        if type(state.active_revision) is not bool:
            raise ApiError(503, "TASK_INTEGRATION_UNAVAILABLE", "Task integration is unavailable.")
        if not state.active_revision:
            raise ApiError(409, "STALE_TASK", "Task belongs to an inactive plan revision.")
        if (
            type(state.target_count) is not int
            or not 1 <= state.target_count <= 20
            or type(state.completed_count) is not int
            or state.completed_count < 0
        ):
            raise ApiError(503, "TASK_INTEGRATION_UNAVAILABLE", "Task integration is unavailable.")
        remaining = max(0, state.target_count - state.completed_count)
        if type(count) is not int or not 1 <= count <= remaining:
            raise ApiError(422, "TASK_TARGET_EXCEEDED", "Question count exceeds the remaining task target.")

    def material_in_use(self, session, user_id, material_id):
        # Unknown A references must block destructive deletion.
        return True

    def read_question_attempts(self, session, user_id, set_id, question_ids):
        """Standalone B has no A attempts. An A adapter returns, per question ID,
        {'latest_attempt': DTO or None, 'has_graded_attempt': bool}, after checking
        owner and set membership. The empty default means no attempts are known;
        it is not authoritative once A is connected.
        """
        return {}
