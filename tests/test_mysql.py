"""Live migration + HTTP E2E, only against an empty, explicitly disposable MySQL DB."""

from pathlib import Path
import os

import pytest
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import make_url

from app.db import Database
from app.models import User, PlanTask
import tests.test_e2e as e2e
from tests.mysql_boundary_checks import (
    assert_mysql_claim_deadlock,
    assert_mysql_exact_jwt_identity,
    assert_mysql_unknown_uses_current_read,
)
from tests.support import test_application as make_application


@pytest.fixture
def mysql_live(tmp_path, monkeypatch):
    url = os.getenv("TEST_MYSQL_URL")
    if not url:
        pytest.skip("No disposable TEST_MYSQL_URL configured; SQLite cannot verify MySQL")
    parsed = make_url(url)
    assert parsed.drivername == "mysql+pymysql" and parsed.database.endswith("_test")
    db = Database(url)
    assert not inspect(db.engine).get_table_names(), "MySQL test refuses nonempty database"
    # Minimal A references are test fixtures, not production migrations.
    with db.engine.begin() as conn:
        for table in (User.__table__, PlanTask.__table__):
            fixture_table = table.to_metadata(MetaData())
            fixture_table.dialect_options["mysql"].update(
                engine="InnoDB", charset="utf8mb4", collate="utf8mb4_bin"
            )
            fixture_table.create(conn)
    root = Path(__file__).resolve().parents[1]
    for name in (
        "001_mysql_b_baseline.sql",
        "002_mysql_job_operational_columns.sql",
        "003_mysql_active_resource.sql",
        "004_mysql_fix_final_checks.sql",
    ):
        source = (root / "migrations" / name).read_text(encoding="utf-8")
        clean = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("--"))
        with db.engine.begin() as conn:
            for statement in clean.split(";"):
                if statement.strip():
                    conn.exec_driver_sql(statement)
    with db.engine.connect() as conn:
        version = conn.execute(text("SELECT VERSION()")).scalar_one()
        assert version.startswith("8."), version
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


def test_mysql_migrations_and_complete_http_pipeline(mysql_live):
    assert_mysql_exact_jwt_identity(mysql_live)
    assert_mysql_claim_deadlock(mysql_live)
    e2e.test_complete_content_pipeline_and_a_boundary(mysql_live)
    assert_mysql_unknown_uses_current_read(mysql_live)
