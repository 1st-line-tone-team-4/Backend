# Team4 Learning Backend — A+B 통합

Python 3.12 / FastAPI 기반 학습 API입니다. A의 목표·계획·채점·Teach-back·학습 기록과 B의 자료·개념·문제·자습 설명을 **하나의 앱, DB, 인증, 비동기 worker**로 실행합니다. 구현된 최종 API 명세는 [`contracts/openapi-final-monthly-subscription.yaml`](contracts/openapi-final-monthly-subscription.yaml)이며, 실행 중인 `/docs`, `/openapi.json`과 [통합 API 안내](docs/API_SPEC_FULL.md)에서도 확인할 수 있습니다.

## 팀 개발 시작

저장소 루트에서 실행합니다. 유일한 의존성 잠금 파일인 `requirements.lock`으로 설치한 다음 editable install로 `app`과 `src/language_learning_api`를 함께 등록해야 합니다. 이전 A 단독 환경의 `uv.lock`/`uv sync` 설치 절차는 통합 서버에 사용하지 않습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_mysql.py
.\.venv\Scripts\python.exe -m ruff check app src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check app src tests scripts
```

Linux/macOS에서는 `.venv/bin/python`을 사용합니다. 테스트는 임시 SQLite·비공개 저장소와 명시적으로 주입한 인증/provider 대역을 사용합니다. HTTP E2E는 loopback 서버와 별도 worker 프로세스를 실행하므로 실행 환경에서 로컬 통신이 가능해야 합니다. 기본 테스트와 CI는 실제 AI 키나 유료 provider 호출을 사용하지 않습니다.

### DB 준비

팀 개발용 MySQL 8.0.16+ 빈 DB를 준비한 뒤 [마이그레이션 안내](migrations/README.md)에 따라 **000 → 001 → 002 → 003 → 004 → 005**를 한 번씩 적용합니다. MySQL 클라이언트가 설치된 PowerShell에서 다음과 같이 파일별 종료 코드를 검사할 수 있습니다. `DB_HOST`, `DB_USER`, `DB_NAME`은 준비한 개발 DB 값으로 바꾸고 비밀번호는 프롬프트에서 입력합니다.

```powershell
$migrationFiles = @(
    '000_mysql_shared_prerequisites.sql', '001_mysql_b_baseline.sql',
    '002_mysql_job_operational_columns.sql', '003_mysql_active_resource.sql',
    '004_mysql_fix_final_checks.sql', '005_mysql_canonical_a_schema.sql'
)
foreach ($migrationFile in $migrationFiles) {
    mysql --batch --skip-force --host=DB_HOST --user=DB_USER --password --database=DB_NAME --execute="source migrations/$migrationFile"
    if ($LASTEXITCODE -ne 0) { throw "Migration failed: $migrationFile" }
}
```

첫 오류에서 중단해야 하므로 오류를 무시하는 `--force`를 사용하지 않습니다. 파일 실행 방식은 [MySQL batch 안내](https://dev.mysql.com/doc/refman/8.0/en/batch-mode.html)를 참고합니다. 기존 B004 DB는 `users`의 PK·인증 식별자 UNIQUE·timezone CHECK까지 별도로 확인한 뒤 005만 적용합니다. 자동 guard는 모든 기존 제약을 검증하지 않습니다. 005는 비어 있는 `plan_tasks(id)` 스텁을 확장하며, 작업 이력이 들어 있는 스텁은 임의로 보충하지 않고 거부합니다. 기존 독립 A DB는 별도 데이터 조정 계획이 필요합니다.

`app.models.Base`가 35개 계약 테이블을 모두 정의합니다. `language_learning_api.models`는 같은 클래스를 다시 노출하는 호환 경로입니다. 실험용 A Alembic 이력은 `migrations/legacy_a_experimental/`에 보존되어 있고 Alembic 실행은 명시적으로 차단됩니다. `alembic upgrade head`나 운영 `create_schema()`를 실행하지 않습니다.

### 환경 설정과 서버 실행

[.env.example](.env.example)은 필요한 변수 목록입니다. 앱은 `.env`를 자동으로 읽지 않으므로 프로세스 환경 또는 secret manager로 설정합니다.

| 변수 | 용도 |
| --- | --- |
| `PROFILE` | 로컬 개발은 `development`, 운영은 `production`; 기본값은 `production` |
| `DATABASE_URL` | `mysql+pymysql://…?charset=utf8mb4` |
| `STORAGE_ROOT`, `PUBLIC_BASE_URL` | 비공개 파일 경로와 클라이언트가 접근할 API 주소 |
| `SIGNING_SECRET` | 외부에서 공급한 32바이트 이상의 비밀값; 저장소 URL·cursor 서명 |
| `CREDENTIAL_ENCRYPTION_KEY` | 선택. 이전 BYOK Job의 남은 credential 정리에 필요한 base64 인코딩 32바이트 AES 키 |
| `AUTH_PUBLIC_KEY`, `AUTH_ISSUER`, `AUTH_AUDIENCE` | 외부 인증 제공자의 RS256 공개키 PEM·issuer·audience |
| `ENABLED_MODELS` | 승인한 `openai:<model>` / `anthropic:<model>` ID의 쉼표 구분 목록; 비어 있으면 활성 모델 없음 |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | 서비스가 Secret Manager에서 공급받는 제공자 키. 요청·DB·로그·Git에 넣지 않음. `ENABLED_MODELS`의 OpenAI/Anthropic 모델은 해당 키가 필요합니다. AI 서버 경유 모델은 AI 서버가 자기 `GEMINI_API_KEY`를 쓰므로(`AI_USE_SERVER_TEXT_KEY=true`) 백엔드 `GEMINI_API_KEY`는 선택이며, 설정하면 AI 서버 키 대신 전달됩니다. 단, 텍스트 Teach-back 답변은 AI 서버에 경로가 없어 백엔드 `GEMINI_API_KEY`가 있어야 합니다 |
| `AI_SERVICE_URL`, `AI_SERVICE_API_KEY` | 선택. AI 서버(`AI` repo) 주소와 서버 간 키(AI 서버의 `AI_SERVER_API_KEY`). 설정하면 PDF/PNG/JPEG 인식을 AI 서버의 Upstage 문서 인식으로 처리하고, 비우면 기존 로컬 추출을 사용합니다. 자세한 내용은 [팀 repo 통합 안내](docs/INTEGRATION_REPOS.md) |
| `TEACHBACK_TEXT_MODEL` | 선택. 텍스트 Teach-back 답변(AI 서버에 경로가 없는 유일한 단계)을 처리할 직접 모델(예: `openai:<model>`). 해당 제공자 키(`OPENAI_API_KEY` 등)가 필요하며, 비우면 백엔드 `GEMINI_API_KEY`로 Gemini를 호출합니다. |
| `AI_SERVICE_MODELS` | 선택. 비우면 모델 목록을 AI 서버의 `GET /internal/models`에서 받아(60초 캐시) 분석·문제 생성·채점·학습 설명·Teach-back 리포트를 내부 API로 처리합니다. 모델 선택은 AI 서버 설정이 결정합니다. 값을 넣으면 그 `gemini:<model>` 목록으로 고정합니다. `AI_SERVICE_URL`이 필요하며 `ENABLED_MODELS`와 겹칠 수 없습니다. |
| `CORS_ALLOWED_ORIGINS` | 선택. 프런트엔드를 API와 다른 origin에서 서비스할 때 허용할 정확한 origin의 쉼표 구분 목록(예: `https://team4.vercel.app`). 경로·`*`·끝 슬래시는 거부하며, `production`에서는 https만 허용합니다. 비우면 CORS 헤더를 보내지 않습니다(같은 origin 프록시 사용). |

필수 설정과 DB를 준비한 후 같은 환경을 사용하는 두 터미널에서 실행합니다.

```sh
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
python -m app.worker
# 필요한 경우 worker 한 회만 실행: 대기 Job 최대 1개와 삭제 outbox 처리
python -m app.worker --once
```

`language_learning_api.main:create_app`도 동일한 factory를 다시 노출합니다. 별도의 A 서버나 A 전용 worker는 없습니다. `/health`는 프로세스 응답 확인용이며 DB·provider 준비 상태를 보증하는 readiness 검사는 아닙니다.

운영 profile은 MySQL과 HTTPS를 요구합니다. TLS 종료 프록시를 사용하면 신뢰하는 프록시만 전달 헤더를 설정하도록 구성해야 합니다. API와 worker는 같은 DB, storage root, 모델 allowlist, 암호화 키를 사용합니다. 현재 로컬 vault/storage adapter는 동일 호스트 또는 공유 private 경로를 전제로 합니다. 다중 호스트 배포용 분산 secret store는 별도 구현이 필요합니다.

## 인증과 프런트엔드 연결

인증 제공자가 발급한 JWT를 `Authorization: Bearer <token>`으로 전달합니다. 서버는 RS256 서명과 `iss`, `aud`, `exp`, `sub`를 검증하며, 처음 인증된 `(issuer, subject)`는 로컬 `users` 행으로 생성합니다. 선택적인 `email`, `name` claim은 길이 검사를 거쳐 첫 생성에 사용합니다. DB collation이 공백이나 대소문자를 같게 보더라도 인증 식별자는 정확히 일치해야 합니다.

**이 서버에는 비밀번호 회원가입·로그인, 토큰 발급·갱신 API가 없습니다.** 팀은 외부 인증 제공자와 클라이언트 로그인 흐름을 먼저 정해야 합니다. `PROFILE=test`만으로 인증 대역이 활성화되지 않으며 테스트 코드가 대역을 명시적으로 주입합니다.

프런트엔드(`frontend` repo)는 기본적으로 같은 origin의 `/v1`을 호출합니다. 로컬 개발에서는 Vite가 `/v1`·`/health`를 `BACKEND_PROXY_TARGET`(기본 `http://127.0.0.1:8000`)으로 프록시하므로 CORS 설정이 필요 없습니다. 배포에서 프런트와 API를 다른 origin에 두면 프런트의 `VITE_API_BASE_URL`을 API의 `/v1` 절대 주소로 바꾸고, 백엔드 `CORS_ALLOWED_ORIGINS`에 프런트 origin을 넣습니다. 서명된 업로드 URL은 `PUBLIC_BASE_URL` 기준으로 만들어지므로 브라우저가 실제로 접근하는 API 주소와 같아야 합니다. 프런트 연동 범위와 검증 결과는 [팀 repo 통합 안내](docs/INTEGRATION_REPOS.md)에 있습니다.

연동 순서는 다음과 같습니다.

1. 외부 JWT로 `GET /v1/me`를 확인합니다.
2. 자료 업로드 → 분석 Job 완료 → 개념 확정으로 B의 실제 자료·버전을 준비합니다.
3. 목표와 학습 계획을 생성하고 반환된 task·개념 버전 ID를 그대로 사용합니다.
4. 문제 생성·채점, Teach-back 메시지·리포트는 `202`의 Job을 조회해 완료를 확인합니다.
5. `/v1/today`와 `/v1/records/*`에서 같은 DB의 결과와 진행 상태를 읽습니다.

AI 단계에는 허용 모델과 서버에 설정된 제공자 키가 필요합니다. 클라이언트는 제공자 키를 입력하거나 `X-AI-Key`로 전송하지 않습니다. 서버는 설정된 키가 없는 모델을 사용 불가로 표시하고 요청을 거부합니다.

제품 과금 방침은 월 구독입니다. 가격·결제사는 아직 정해지지 않아 checkout과 실제 구독 권한 검증은 후속 연동 범위입니다. `GET /v1/billing/capabilities`는 `pricing_pending`, `checkout_enabled=false`를 반환합니다. 모든 AI 생성은 서버 관리 제공자 키를 사용합니다. 중복 실행 동의는 결과가 불확실한 작업의 AI 재실행 가능성에 대한 것이며 구독료 외 별도 결제를 뜻하지 않습니다.

보고서 작업이 종료된 `report_pending` 세션은 저장된 보고서가 없으면 `{status:"abandoned"}` PATCH로 포기할 수 있습니다. 실행 중 작업은 `409`로 보호하고, 완료된 보고서는 변경하지 않습니다. 포기 재시도는 `200`이며, 기존 대화·Job 포인터·실패 이력을 남깁니다. 연결된 계획이 바뀌어도 포기 재시도가 가능하며 학습 완료량을 바꾸지 않습니다.

## 공유 처리와 보안 경계

- POST는 `Idempotency-Key`를 사용합니다. 같은 키·같은 요청은 재생하고 다른 내용은 `409`로 거부합니다. 키 길이는 16–128자이며 기록은 24시간 유효합니다. PATCH에 POST 멱등성 규칙을 추가하지 않았습니다.
- 모든 A/B 비동기 작업은 `ai_jobs`와 같은 JobService를 사용합니다. 등록 종류는 `analysis`, `questions`, `learning_explanation`, `grade`, `message`, `report`입니다. `202`는 접수 응답이며 결과 생성 완료가 아닙니다.
- worker는 lease/fencing과 현재 Job 포인터를 검사합니다. provider 결과가 불확실하면 `outcome_unknown`으로 남기고 자동 재호출하지 않습니다. retry는 재시도 가능한 확정 실패에만 허용되며, restart는 서비스 관리 키를 사용하고 `acknowledge_possible_duplicate_execution=true`가 필요합니다. 이 동의는 사용자 추가 결제를 뜻하지 않습니다.
- 제공자 키는 Secret Manager에서 서버에 공급하고 도메인 DB·로그·Git·클라이언트에 저장하지 않습니다. 프록시도 Authorization과 signed URL query를 기록하지 않도록 설정합니다. 구형 Job의 credential reference는 새 AI 제공에 사용하지 않습니다.
- 소스 파일은 비공개 저장소와 목적·소유자·만료가 묶인 서명 URL로 접근합니다. 자료 삭제는 참조 검증과 outbox를 거치며 worker가 실제 파일을 제거합니다. 인증 없는 소스 접근이나 임의 외부 URL 가져오기는 제공하지 않습니다.
- 확인된 개념 버전과 evidence는 고정됩니다. 계획의 개념 정보와 리포트 항목은 정규화된 행에서 읽으며 JSON 복제본이나 별도 A 테이블에 B 데이터를 복사하지 않습니다. 자습 설명 생성은 task 완료량이나 coverage를 바꾸지 않습니다.

provider 생성 요청의 전체 제한은 300초이며 기본 worker lease는 420초입니다. 준비 예산 60초를 포함해 생성자에서 `420 > 300 + 60` 조건을 검증합니다. 이는 대기열부터 결과까지의 전체 완료 시간을 보증하는 수치가 아닙니다. 업로드·출력 크기와 파서 제한, 불확실한 COMMIT 처리 등 기존 보안 경계는 [추가 검수 기록](docs/REVIEW-N-FIXES.md)에 정리되어 있습니다.

## 제공 범위와 검증 자료

현재 전체 계약은 49개 경로·63개 작업이고, 통합 서버는 그중 40개 경로·51개 작업을 게시합니다. 월 구독 상태를 알리는 `GET /billing/capabilities`는 구현되어 있지만 실제 checkout과 구독 권한 검증은 아직 없습니다. 공개 게시·공개 풀이 생성·점수 인증과 결제 연동의 미구현 범위는 [통합 검증 기록](docs/INTEGRATION_VALIDATION.md)을 참고하세요. 35개 테이블이 있다는 사실이 빠진 기능의 구현을 뜻하지는 않습니다.

`GET /v1/records/public-attempts`는 구현되어 있으며 소유자의 저장된 개인 snapshot을 조회합니다. 적법하게 준비하거나 가져온 기존 데이터가 있으면 결과를 반환하므로 항상 빈 목록이라고 가정하지 않습니다. 이 조회가 공개 게시나 새로운 공개 풀이 생성 API를 대신하지는 않습니다.

- [통합 API 경로·상태 안내](docs/API_SPEC_FULL.md), [A 학습 흐름 안내](docs/A_API_SPEC.md)
- [모듈 책임·요청 흐름·호환 경계](docs/ARCHITECTURE.md)
- [구현된 최종 월 구독형 OpenAPI 명세](contracts/openapi-final-monthly-subscription.yaml), [전체 월 구독형 계약 JSON](contracts/openapi-1.4.0-monthly-subscription.json), [기반 SQL schema proposal](contracts/schema-proposal-1.3.0.sql)
- [마이그레이션 및 기존 DB 제한](migrations/README.md), [통합 검증 및 미결 정책](docs/INTEGRATION_VALIDATION.md), [과거 B 검증 기록](docs/VALIDATION.md)

CI는 editable install 후 SQLite 회귀와 별도의 disposable MySQL 8.0 검증을 실행하도록 구성되어 있습니다. MySQL fixture는 000–005 체인, 기존 B 데이터 보존, 비어 있지 않은 task 스텁 거부를 확인합니다. 로컬 `TEST_MYSQL_URL`이 없으면 해당 테스트는 명시적으로 skip합니다. 과거 B 전용 SHA의 성공은 현재 통합 SHA나 운영 DB 호환성을 입증하지 않으므로 최종 통합 검증 결과를 별도로 확인해야 합니다.
