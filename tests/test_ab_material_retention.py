"""Material deletion must not wait for retained A history while holding source locks."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.a_gateway import CanonicalTaskGateway
from app.models import Material, StorageDeletion, TeachbackSession
from tests.support import test_application as make_application
from tests.test_ab_plans import OWNER, material, post


@pytest.mark.parametrize("table", ["study_plan_materials", "plan_task_concepts", "teachback_sessions"])
def test_contended_history_probe_rolls_back_deletion(tmp_path, table):
    fixture = make_application(tmp_path)
    fixture.owner = fixture.auth.tokens["test-owner-token"]
    fixture.ctx.task_gateway = CanonicalTaskGateway()
    source = material(fixture)

    def contended(connection, cursor, statement, parameters, context, many):
        if "FROM " + table in statement:
            raise OperationalError(statement, parameters, Exception(3572, "test-only locked history"))

    event.listen(fixture.ctx.db.engine, "before_cursor_execute", contended)
    try:
        with TestClient(fixture.app, headers=OWNER) as client:
            response = client.delete("/v1/materials/" + source.id)
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "MATERIAL_IN_USE"
        with fixture.ctx.db.transaction() as db:
            assert db.get(Material, source.id) is not None
            assert db.scalar(select(func.count()).select_from(StorageDeletion)) == 0
    finally:
        event.remove(fixture.ctx.db.engine, "before_cursor_execute", contended)
        fixture.ctx.db.engine.dispose()


def assert_mysql_retained_session_does_not_deadlock_delete(fixture):
    assert fixture.ctx.db.engine.dialect.name == "mysql"
    source = material(fixture)
    response = post(
        fixture,
        "/v1/teachback-sessions",
        {
            "concept_version_id": source.version_id,
            "explanation_language": "en",
            "student_persona": "curious_beginner",
        },
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["data"]["id"]
    with fixture.ctx.db.transaction() as worker:
        worker.scalar(select(TeachbackSession).where(TeachbackSession.id == session_id).with_for_update())
        deleted = fixture.client.delete("/v1/materials/" + source.id)
        assert deleted.status_code == 409, deleted.text
        assert deleted.json()["error"]["code"] == "MATERIAL_IN_USE"
        # The rejected deletion has released its material lock. This also proves
        # the worker can continue toward its FK-protected report persistence.
        assert (
            worker.scalar(select(Material.id).where(Material.id == source.id).with_for_update(nowait=True))
            == source.id
        )
    with fixture.ctx.db.transaction() as db:
        assert db.get(TeachbackSession, session_id) is not None
        assert db.get(Material, source.id) is not None
