"""Live migration + HTTP E2E, only against an empty, explicitly disposable MySQL DB."""

from pathlib import Path
import os

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core import uid, utcnow
from app.db import Database
from app.models import Base, Job, Material, User
import tests.test_e2e as e2e
from tests.mysql_boundary_checks import (
    assert_mysql_claim_deadlock,
    assert_mysql_exact_jwt_identity,
    assert_mysql_unknown_uses_current_read,
)
from tests.support import test_application as make_application
from tests.review_n_boundary_checks import (
    assert_analysis_preparation_releases_locks,
    assert_key_validation_releases_transaction,
    assert_mysql_delete_does_not_gap_lock_first_complete,
    assert_unknown_job_audit_survives_material_deletion,
)
from tests.test_key_validation_n import assert_key_validation_claim_race
from tests.ab_support import ab_http_application
from tests.test_ab_e2e import assert_ab_http_pipeline
from tests.test_terminal_jobs_n11 import assert_mysql_terminal_history_current_reads
from tests.test_plan_queries_n14 import assert_mysql_batched_progress_current_read
from tests.test_ab_teachback import assert_mysql_standalone_link_lock_order
from tests.test_ab_material_retention import assert_mysql_retained_session_does_not_deadlock_delete
from tests.test_ab_plans import (
    assert_mysql_generation_acceptance_after_lock_wait,
    assert_mysql_plan_reschedule_completion,
)


def _execute_migration(connection, name):
    source = (Path(__file__).resolve().parents[1] / "migrations" / name).read_text(encoding="utf-8")
    clean = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("--"))
    for statement in clean.split(";"):
        if statement.strip():
            connection.exec_driver_sql(statement)


def _b_snapshot(connection, ids):
    """Read exact persisted values, including MySQL timestamp/JSON conversion."""
    return {
        table: dict(
            connection.execute(text(f"SELECT * FROM {table} WHERE id = :id"), {"id": ids[table]})
            .mappings()
            .one()
        )
        for table in ("users", "materials", "ai_jobs")
    }


def _assert_b_upgrade_boundary(db):
    """Exercise a populated B004 upgrade and reject incomplete A history first."""
    ids = {table: uid() for table in ("users", "materials", "ai_jobs")}
    stub_id, now = uid(), utcnow()
    with db.transaction() as session:
        session.add(
            User(
                id=ids["users"],
                auth_issuer="migration-only",
                auth_subject=ids["users"],
                display_name="Preserved B owner",
                created_at=now,
            )
        )
        session.flush()
        session.add(
            Job(
                id=ids["ai_jobs"],
                user_id=ids["users"],
                kind="analysis",
                status="failed",
                resource_type="material",
                resource_id=ids["materials"],
                request_fingerprint="b" * 64,
                request_payload={"fixture": "preserve-before-A-integration"},
                model_id="test:grounded",
                attempt_no=1,
                retryable=False,
                error_code="INVALID_OUTPUT",
                fencing_token=7,
                dispatched_at=now,
                last_polled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            Material(
                id=ids["materials"],
                user_id=ids["users"],
                filename="preserved.txt",
                mime_type="text/plain",
                size_bytes=15,
                storage_key="migration-fixture/" + uid(),
                processing_status="failed",
                archived=True,
                latest_job_id=ids["ai_jobs"],
                created_at=now,
                updated_at=now,
            )
        )
        # This is intentionally a pre-005 table. Never use the full ORM model
        # to fabricate missing goal/plan/pin provenance in the old ID-only stub.
        session.execute(text("INSERT INTO plan_tasks (id) VALUES (:id)"), {"id": stub_id})

    with db.engine.connect() as connection:
        before = _b_snapshot(connection, ids)
        before_tables = set(inspect(connection).get_table_names())
        assert len(before_tables) == 20
        connection.rollback()
        try:
            with pytest.raises(IntegrityError) as rejected:
                _execute_migration(connection, "005_mysql_canonical_a_schema.sql")
            assert rejected.value.orig.args[0] == 1062
            assert "FROM plan_tasks LIMIT 1" in rejected.value.statement
            connection.rollback()
            assert set(inspect(connection).get_table_names()) == before_tables
            assert [column["name"] for column in inspect(connection).get_columns("plan_tasks")] == ["id"]
            assert _b_snapshot(connection, ids) == before
            assert (
                connection.execute(
                    text("SELECT id FROM plan_tasks WHERE id = :id"), {"id": stub_id}
                ).scalar_one()
                == stub_id
            )
        finally:
            # Temporary tables survive transaction rollback and a pooled
            # connection close. Clean this guard on its exact connection.
            connection.rollback()
            connection.exec_driver_sql("DROP TEMPORARY TABLE IF EXISTS ab_005_preflight")
            connection.commit()

    # The test owns this exact synthetic row in a database proven empty at entry.
    # Production/nonempty task history must instead receive authoritative backfill.
    with db.engine.begin() as connection:
        result = connection.execute(text("DELETE FROM plan_tasks WHERE id = :id"), {"id": stub_id})
        assert result.rowcount == 1
    with db.engine.begin() as connection:
        _execute_migration(connection, "005_mysql_canonical_a_schema.sql")
    with db.engine.connect() as connection:
        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == set(Base.metadata.tables)
        assert len(inspector.get_table_names()) == 35
        for table in Base.metadata.tables.values():
            assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.c.keys())
        assert _b_snapshot(connection, ids) == before
        task_fks = inspector.get_foreign_keys("plan_tasks")
        assert any(fk["referred_table"] == "study_plans" for fk in task_fks)
        set_fks = inspector.get_foreign_keys("question_sets")
        assert any(fk["referred_table"] == "plan_tasks" for fk in set_fks)
        attempt_columns = {column["name"]: column for column in inspector.get_columns("question_attempts")}
        assert attempt_columns["answered_at"]["type"].fsp == 3
        report_columns = {column["name"]: column for column in inspector.get_columns("session_reports")}
        assert (report_columns["coverage"]["type"].precision, report_columns["coverage"]["type"].scale) == (
            5,
            4,
        )


@pytest.fixture
def mysql_live(tmp_path, monkeypatch):
    url = os.getenv("TEST_MYSQL_URL")
    if not url:
        pytest.skip("No disposable TEST_MYSQL_URL configured; SQLite cannot verify MySQL")
    parsed = make_url(url)
    assert parsed.drivername == "mysql+pymysql" and parsed.database and parsed.database.endswith("_test")
    db = Database(url)
    assert not inspect(db.engine).get_table_names(), "MySQL test refuses nonempty database"
    with db.engine.connect() as conn:
        version = conn.execute(text("SELECT VERSION()")).scalar_one()
        assert version.startswith("8."), version
    for name in (
        "000_mysql_shared_prerequisites.sql",
        "001_mysql_b_baseline.sql",
        "002_mysql_job_operational_columns.sql",
        "003_mysql_active_resource.sql",
        "004_mysql_fix_final_checks.sql",
    ):
        with db.engine.begin() as conn:
            _execute_migration(conn, name)
    _assert_b_upgrade_boundary(db)
    with db.engine.connect() as conn:
        column = (
            conn.exec_driver_sql("SHOW COLUMNS FROM learning_explanations LIKE 'content_markdown'")
            .mappings()
            .one()
        )
        assert column["Type"] == "longtext"
    monkeypatch.setattr(
        e2e,
        "make_test_application",
        lambda path, base_url: make_application(path, base_url, database_url=url, create_schema=False),
    )
    fixture = e2e.live.__wrapped__(tmp_path)
    value = next(fixture)
    try:
        yield value
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
        db.engine.dispose()


def test_mysql_migrations_and_complete_http_pipeline(mysql_live, tmp_path):
    # Same migrated canonical schema; a separate app verifies real signed JWTs
    # and the default task gateway. Run first, before boundary fixtures leave
    # deliberately queued/unknown jobs for their own credential stores.
    with ab_http_application(
        tmp_path / "ab-http", database_url=mysql_live.ctx.settings.database_url, create_schema=False
    ) as integrated:
        pipeline = assert_ab_http_pipeline(integrated)
        assert_mysql_standalone_link_lock_order(integrated, pipeline)
        assert_mysql_retained_session_does_not_deadlock_delete(integrated)
        assert_mysql_terminal_history_current_reads(integrated)
        assert_mysql_batched_progress_current_read(integrated)
    assert_mysql_exact_jwt_identity(mysql_live)
    assert_mysql_claim_deadlock(mysql_live)
    e2e.test_complete_content_pipeline_and_a_boundary(mysql_live)
    assert_mysql_unknown_uses_current_read(mysql_live)
    assert_key_validation_releases_transaction(mysql_live)
    assert_analysis_preparation_releases_locks(mysql_live)
    owner = mysql_live.auth.tokens["test-owner-token"]
    assert_key_validation_claim_race(mysql_live.ctx, owner, expired=False)
    assert_key_validation_claim_race(mysql_live.ctx, owner, expired=True)
    assert_mysql_delete_does_not_gap_lock_first_complete(mysql_live)
    assert_unknown_job_audit_survives_material_deletion(mysql_live)
    assert_mysql_generation_acceptance_after_lock_wait(mysql_live)
    # Last: this fixture deliberately retains a queued grading job while
    # proving rescheduling refuses the worker's Job lock without a deadlock.
    assert_mysql_plan_reschedule_completion(mysql_live)
