from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from sqlalchemy.exc import IntegrityError
from app.auth import JWTAuthenticator
from app.core import ApiError, Settings, idempotent, uid, utcnow
from app.db import Database
from app.models import User, Material, MaterialUnit, Concept
from app.ports import TaskGateway

@pytest.fixture
def context(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'db.sqlite'))
    db.create_schema()
    user = uid()
    with db.transaction() as s:
        s.add(User(id=user, auth_issuer='issuer', auth_subject='subject', created_at=utcnow()))
    return SimpleNamespace(db=db, user=user)

def test_idempotent_replay_conflict_and_rollback(context):
    ctx = context
    calls = []
    def action(s):
        calls.append(1)
        return 201, {'id': 'one'}
    args = (ctx, ctx.user, 'create', 'a' * 16)
    assert idempotent(*args, {'body': 1}, action).status_code == 201
    assert idempotent(*args, {'body': 1}, action).status_code == 201
    assert len(calls) == 1
    with pytest.raises(ApiError) as exc:
        idempotent(*args, {'body': 2}, action)
    assert exc.value.status == 409
    def failure(s):
        raise RuntimeError('fail')
    with pytest.raises(RuntimeError):
        idempotent(ctx, ctx.user, 'other', 'b'*16, {}, failure)
    assert idempotent(ctx, ctx.user, 'other', 'b'*16, {}, action).status_code == 201

def test_composite_material_constraint(context):
    with pytest.raises(IntegrityError):
        with context.db.transaction() as s:
            now = utcnow()
            for i in ('a', 'b'):
                s.add(Material(id=i, user_id=context.user, filename='a', mime_type='text/plain', size_bytes=1, storage_key=i, processing_status='ready', created_at=now, updated_at=now))
            s.flush()
            s.add(MaterialUnit(id='unit', material_id='a', ordinal=1, title='x'))
            s.flush()
            s.add(Concept(id='concept', material_id='b', unit_id='unit', created_at=now))
            s.flush()

def test_auth_pins_algorithm_claims_and_existing_identity(context):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    auth = JWTAuthenticator(Settings(auth_public_key=public, auth_issuer='issuer', auth_audience='aud'), context.db)
    def request(token): return SimpleNamespace(headers={'Authorization': 'Bearer ' + token})
    claims = {'iss':'issuer', 'aud':'aud', 'sub':'subject', 'exp':datetime.now(timezone.utc)+timedelta(minutes=5)}
    assert auth.authenticate(request(jwt.encode(claims, private, algorithm='RS256'))) == context.user
    for changes in ({'aud':'wrong'}, {'sub':'unknown'}, {'exp':datetime.now(timezone.utc)-timedelta(seconds=1)}):
        with pytest.raises(ApiError): auth.authenticate(request(jwt.encode({**claims, **changes}, private, algorithm='RS256')))
    with pytest.raises(ApiError): auth.authenticate(request(jwt.encode(claims, 'attacker-secret-which-is-long-enough-for-hs256', algorithm='HS256')))

def test_unconfigured_task_gateway_denies():
    gateway = TaskGateway()
    assert gateway.material_in_use(None, 'user', 'material')
    with pytest.raises(ApiError) as exc:
        gateway.validate_question_task(None, 'user', 'task', 'unit', [])
    assert exc.value.status == 503

def test_expired_key_replacement_and_concurrent_replay(context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from app.models import IdempotencyRequest
    key = 'e' * 16
    calls = []
    def old_action(s): return 201, {'version': 'old'}
    idempotent(context, context.user, 'expire', key, {'version': 'old'}, old_action)
    with context.db.transaction() as s:
        s.get(IdempotencyRequest, (context.user, 'expire', key)).expires_at = utcnow() - timedelta(seconds=1)
    barrier, lock = Barrier(2), Lock()
    def action(s):
        with lock: calls.append(1)
        return 202, {'version': 'new'}
    def run():
        barrier.wait()
        return idempotent(context, context.user, 'expire', key, {'version': 'new'}, action)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert [result.status_code for result in results] == [202, 202]
    assert len(calls) == 1

def test_in_progress_retry_after(context):
    from app.core import error_response
    from app.models import IdempotencyRequest
    import hashlib, json
    key = 'p'*16
    digest = hashlib.sha256(json.dumps({}, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    with context.db.transaction() as s:
        s.add(IdempotencyRequest(user_id=context.user, operation_id='pending', idempotency_key=key, request_hash=digest, status='in_progress', created_at=utcnow(), expires_at=utcnow()+timedelta(hours=1)))
    with pytest.raises(ApiError) as exc:
        idempotent(context, context.user, 'pending', key, {}, lambda s: (200, {}))
    response = error_response(SimpleNamespace(state=SimpleNamespace()), exc.value)
    assert exc.value.code == 'REQUEST_IN_PROGRESS'
    assert response.headers['Retry-After'] == '2'


def test_sqlite_character_length_is_unicode_aware(context):
    from sqlalchemy import text
    with context.db.transaction() as s:
        assert s.scalar(text("select char_length(:value)"), {'value': '한글'}) == 2

def test_mysql_migration_compiles_and_preserves_boundaries():
    from pathlib import Path
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import mysql
    from app.models import Base
    ddl = {t.name: str(CreateTable(t).compile(dialect=mysql.dialect())) for t in Base.metadata.sorted_tables}
    assert 'LONGTEXT' in ddl['material_chunks']
    assert 'LONGTEXT' in ddl['learning_explanations']
    assert 'LONGTEXT' in ddl['questions']
    assert 'DATETIME(3)' in ddl['ai_jobs']
    assert 'char_length(' in ddl['learning_explanations']
    baseline = Path('migrations/001_mysql_b_baseline.sql').read_text(encoding='utf-8')
    for name in ddl:
        if name not in ('users', 'plan_tasks'):
            assert f'CREATE TABLE {name} (' in baseline
    assert 'CREATE TABLE users (' not in baseline
    assert 'CREATE TABLE plan_tasks (' not in baseline
    assert baseline.count('COLLATE utf8mb4_bin') == len(ddl) - 2
    assert 'dispatched_at' not in baseline
    additive = Path('migrations/002_mysql_job_operational_columns.sql').read_text(encoding='utf-8')
    assert all(c in additive for c in ('dispatched_at', 'credential_ref', 'last_polled_at'))

def test_standalone_gateway_reports_no_known_attempts():
    assert TaskGateway().read_question_attempts(None, 'user', 'set', ['question']) == {}

def test_one_active_job_per_resource(context):
    from app.models import Job
    now = utcnow()
    def make_job(job_id, fingerprint, status):
        return Job(id=job_id, user_id=context.user, kind='questions', status=status, resource_type='question_set', resource_id='same', request_fingerprint=fingerprint, request_payload={}, model_id='model', attempt_no=1, retryable=False, fencing_token=0, created_at=now, updated_at=now)
    with context.db.transaction() as s:
        s.add(make_job('job1', 'a'*64, 'queued'))
    with pytest.raises(IntegrityError):
        with context.db.transaction() as s:
            s.add(make_job('job2', 'b'*64, 'running'))
            s.flush()
    with context.db.transaction() as s:
        s.get(Job, 'job1').status = 'failed'
    with context.db.transaction() as s:
        s.add(make_job('job2', 'b'*64, 'running'))

def test_mysql_idempotency_key_uses_no_pad_binary_collation():
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import mysql
    from app.models import IdempotencyRequest
    ddl = str(CreateTable(IdempotencyRequest.__table__).compile(dialect=mysql.dialect()))
    assert 'utf8mb4_0900_bin' in ddl
