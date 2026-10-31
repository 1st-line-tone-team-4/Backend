from .core import ApiError


class TaskGateway:
    """A integration boundary. An unconfigured adapter denies task usage and deletion."""

    def validate_question_task(self, session, user_id, task_id, unit_id, concept_version_ids):
        raise ApiError(503, "TASK_INTEGRATION_UNAVAILABLE", "Task integration is unavailable.")

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
