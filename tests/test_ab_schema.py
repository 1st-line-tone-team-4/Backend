"""Contract/constraint tests for the shared A+B persistence boundary."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import re
from uuid import UUID

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app.core import uid, utcnow
from app.db import Database
from app.models import (
    Base,
    Concept,
    ConceptChecklistItem,
    ConceptVersion,
    ExamGoal,
    Material,
    MaterialUnit,
    PlanTask,
    PlanTaskConcept,
    ReportItem,
    SessionReport,
    StudyPlan,
    TeachbackSession,
    User,
)


ROOT = Path(__file__).resolve().parents[1]


def _names(value):
    return tuple(re.findall(r"`([^`]+)`", value))


def _normalized_check(value):
    # SQLite needs decimal coercion; this is the same MySQL DECIMAL formula.
    return re.sub(r"[\s`]+", "", value).lower().replace("*1.0", "")


def test_single_registry_matches_every_canonical_table_and_constraint():
    from language_learning_api import models as a_models
    from language_learning_api.database import Base as a_base

    assert a_base is Base and a_models.Base is Base
    assert a_models.Material is Material and a_models.PlanTask is PlanTask
    assert a_models.Unit is MaterialUnit
    sql = (ROOT / "contracts/schema-proposal-1.3.0.sql").read_text(encoding="utf-8")
    expected = dict(re.findall(r"CREATE TABLE `([^`]+)` \((.*?)\n\) ENGINE=", sql, re.S))
    assert len(expected) == 36 and expected.keys() == Base.metadata.tables.keys()
    operational_extras = {
        "ai_jobs": {"dispatched_at", "credential_ref", "last_polled_at", "active_resource_id"},
    }
    for name, body in expected.items():
        table = Base.metadata.tables[name]
        columns = dict(re.findall(r"^  `([^`]+)` (.*?)(?:,)?$", body, re.M))
        assert set(table.c.keys()) == columns.keys() | operational_extras.get(name, set()), name
        for column_name, declaration in columns.items():
            column = table.c[column_name]
            if "GENERATED ALWAYS" not in declaration:
                assert column.nullable == ("NOT NULL" not in declaration), (name, column_name)
            if declaration.startswith("CHAR(36)"):
                assert str(column.type.compile(dialect=mysql.dialect())) == "CHAR(36)"
            if declaration.startswith("DECIMAL("):
                precision, scale = map(int, re.search(r"DECIMAL\((\d+),(\d+)\)", declaration).groups())
                assert (column.type.precision, column.type.scale) == (precision, scale)
        primary_key = _names(re.search(r"PRIMARY KEY \((.*?)\)", body).group(1))
        assert tuple(table.primary_key.columns.keys()) == primary_key, name
        expected_unique = {_names(value) for value in re.findall(r"UNIQUE KEY `[^`]+` \((.*?)\)", body)}
        actual_unique = {
            tuple(value.columns.keys()) for value in table.constraints if isinstance(value, UniqueConstraint)
        }
        assert expected_unique <= actual_unique, name
        expected_fks = {
            (_names(local), remote, _names(target))
            for local, remote, target in re.findall(
                r"FOREIGN KEY \((.*?)\) REFERENCES `([^`]+)` \((.*?)\)", body
            )
        }
        actual_fks = {
            (tuple(fk.column_keys), fk.referred_table.name, tuple(item.column.name for item in fk.elements))
            for fk in table.foreign_key_constraints
        }
        assert expected_fks == actual_fks, name
        assert all(fk.ondelete == fk.onupdate == "RESTRICT" for fk in table.foreign_key_constraints)
        checks = {
            _normalized_check(str(value.sqltext))
            for value in table.constraints
            if isinstance(value, CheckConstraint)
        }
        expected_checks = {
            _normalized_check(value) for value in re.findall(r"CONSTRAINT `ck_[^`]+` CHECK \((.*)\)", body)
        }
        assert expected_checks <= checks, name
        for column_name, declaration in columns.items():
            enum = re.match(r"ENUM\((.*?)\)", declaration)
            if enum:
                assert _normalized_check(f"{column_name} IN ({enum.group(1)})") in checks


def test_mysql_chain_covers_full_schema_without_recreating_task_history():
    files = sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_mysql_*.sql"))
    assert [path.name[:3] for path in files] == ["000", "001", "002", "003", "004", "005", "006", "007"]
    sql = "\n".join(path.read_text(encoding="utf-8") for path in files)
    created = re.findall(r"CREATE TABLE (\w+)\s*\(", sql)
    assert len(created) == len(set(created)) == 36
    assert set(created) == Base.metadata.tables.keys()
    migration = files[5].read_text(encoding="utf-8")
    assert "INSERT INTO ab_005_preflight SELECT 0 FROM plan_tasks LIMIT 1" in migration
    assert migration.index("FROM plan_tasks LIMIT 1") < migration.index("CREATE TABLE exam_goals")
    assert "DROP TABLE plan_tasks" not in migration and "CREATE TABLE plan_tasks" not in migration
    assert "ALTER TABLE plan_tasks" in migration
    for column in PlanTask.__table__.columns:
        if column.name != "id":
            assert f"ADD COLUMN {column.name} " in migration
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=mysql.dialect()))
    assert not list((ROOT / "migrations" / "versions").glob("*.py"))
    assert len(list((ROOT / "migrations" / "legacy_a_experimental").glob("*.py"))) == 3


@pytest.fixture
def graph():
    db = Database("sqlite://")
    db.create_schema()
    ids = {
        name: uid()
        for name in (
            "user",
            "goal",
            "plan",
            "task",
            "material",
            "unit",
            "concept",
            "other_concept",
            "version",
            "other_version",
            "check",
            "other_check",
            "session",
        )
    }
    now = utcnow()
    with db.transaction() as session:
        session.add(User(id=ids["user"], auth_issuer="schema", auth_subject="owner", created_at=now))
        session.flush()
        session.add(
            ExamGoal(
                id=ids["goal"],
                user_id=ids["user"],
                title="Exam",
                exam_type="other",
                exam_date=date(2026, 11, 10),
                status="active",
            )
        )
        session.add(
            Material(
                id=ids["material"],
                user_id=ids["user"],
                filename="notes.txt",
                mime_type="text/plain",
                size_bytes=10,
                storage_key=uid(),
                processing_status="ready",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            StudyPlan(
                id=ids["plan"], user_id=ids["user"], exam_goal_id=ids["goal"], revision=1, status="active"
            )
        )
        session.add(MaterialUnit(id=ids["unit"], material_id=ids["material"], ordinal=1, title="Unit"))
        session.flush()
        for label in ("", "other_"):
            session.add(
                Concept(
                    id=ids[label + "concept"],
                    material_id=ids["material"],
                    unit_id=ids["unit"],
                    created_at=now,
                )
            )
        session.add(
            PlanTask(
                id=ids["task"],
                study_plan_id=ids["plan"],
                ordinal=1,
                due_date=date(2026, 11, 2),
                task_type="teach_back",
                target_count=1,
                status="pending",
            )
        )
        session.flush()
        for label in ("", "other_"):
            session.add(
                ConceptVersion(
                    id=ids[label + "version"],
                    concept_id=ids[label + "concept"],
                    version_no=1,
                    name="Pinned",
                    summary="A confirmed explanation.",
                    is_confirmed=True,
                    confirmed_at=now,
                    created_at=now,
                )
            )
        session.flush()
        for label in ("", "other_"):
            session.add(
                ConceptChecklistItem(
                    id=ids[label + "check"],
                    concept_version_id=ids[label + "version"],
                    ordinal=1,
                    criterion="Explain the idea",
                )
            )
        session.add(
            TeachbackSession(
                id=ids["session"],
                user_id=ids["user"],
                concept_version_id=ids["version"],
                task_id=ids["task"],
                explanation_language="en",
                student_persona="curious_beginner",
                status="active",
                question_count=0,
            )
        )
    yield db, ids
    db.engine.dispose()


def test_plan_requires_provenance_and_only_one_active_revision(graph):
    db, ids = graph
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.add(PlanTask(id=uid()))
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.add(StudyPlan(user_id=ids["user"], exam_goal_id=ids["goal"], revision=2, status="active"))
    with db.transaction() as session:
        session.get(StudyPlan, ids["plan"]).status = "superseded"
        session.flush()
        session.add(StudyPlan(user_id=ids["user"], exam_goal_id=ids["goal"], revision=2, status="active"))
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.add(
                PlanTaskConcept(
                    task_id=ids["task"], concept_id=ids["concept"], concept_version_id=ids["other_version"]
                )
            )


def test_report_coverage_and_checklist_pin_are_database_constraints(graph):
    db, ids = graph
    report_id = uid()
    with db.transaction() as session:
        session.add(
            SessionReport(
                id=report_id,
                session_id=ids["session"],
                concept_version_id=ids["version"],
                coverage=Decimal("0.3333"),
                coverage_formula="weighted-v1",
                assessable_count=3,
                weighted_score=Decimal("1.00"),
                next_action="Review the missing ideas",
            )
        )
    with db.transaction() as session:
        report = session.get(SessionReport, report_id)
        assert report.coverage == Decimal("0.3333")
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.get(SessionReport, report_id).coverage = Decimal("0.5000")
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.add(
                ReportItem(
                    report_id=report_id,
                    concept_version_id=ids["version"],
                    checklist_item_id=ids["other_check"],
                    status="explained",
                    description="wrong version",
                    confidence="high",
                )
            )
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.get(TeachbackSession, ids["session"]).status = "completed"


def test_uuid_object_binds_preserve_string_storage(graph):
    db, ids = graph
    with db.transaction() as session:
        assert session.get(User, UUID(ids["user"])).id == ids["user"]
        assert isinstance(session.get(User, UUID(ids["user"])).id, str)
    assert len(inspect(db.engine).get_table_names()) == 36
