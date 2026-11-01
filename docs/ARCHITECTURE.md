# A+B 통합 백엔드 구조

이 문서는 실행 경로와 변경 경계를 빠르게 찾기 위한 소스 지도입니다. 설치는 [README](../README.md), 공개 필드·상태는 [통합 API 안내](API_SPEC_FULL.md)와 [계약 OpenAPI](../contracts/openapi-1.3.0.json)를 기준으로 합니다. 테스트 통과나 이 지도만으로 결함 부재를 보장하지 않습니다.

계약 제안은 **50개 경로 / 64개 작업**, 현재 등록한 계약 API는 **40개 경로 / 51개 작업**입니다. 공개 게시·공개 풀이 생성·점수 인증/관리자 심사·결제 관련 **13개 작업은 미구현**입니다. `/records/public-attempts`는 기존 개인 이력 조회만 제공합니다. `/health`와 서명된 private-storage PUT/GET은 계약 API 집계와 별도입니다.

## 책임 지도

| 영역 | 진입점과 책임 |
| --- | --- |
| 앱 조립 | [`app/main.py`](../app/main.py)의 `create_app`/`build_context`: A/B router, 인증, DB, storage, provider, gateway, handler를 한 context로 연결합니다. |
| 공통 기반 | [`app/core.py`](../app/core.py): 설정·시간/ID·오류·요청 envelope·JSON 검증·멱등성. [`app/db.py`](../app/db.py): transaction과 commit/rollback/불확실한 commit의 외부 cleanup. [`app/auth.py`](../app/auth.py): 외부 JWT 검증과 최초 사용자 연결. |
| 데이터 원본 | [`app/models.py`](../app/models.py)의 **단일 `Base`, 35개 계약 테이블**. 활성 SQL migration은 000→005이며, 별도 A engine/ORM registry는 없습니다. |
| 자료와 생성 콘텐츠 | [`app/materials.py`](../app/materials.py): 업로드·개념·분석 handler. [`app/content.py`](../app/content.py): 문제·자습 설명 API/handler. 둘 다 재사용 조회·응답 변환도 포함합니다. |
| 공유 조회·페이지 | [`app/projections.py`](../app/projections.py): evidence·문제·시도의 응답 변환과 batch loading. [`app/pagination.py`](../app/pagination.py): 기존 세 가지 signed cursor 형식을 유지하는 공통 codec. |
| A 학습 흐름 | [`routers/`](../src/language_learning_api/routers/): 목표·계획·답안·Teach-back·기록 API. [`services/`](../src/language_learning_api/services/): 채점과 Teach-back의 준비·결과 검증·저장. |
| A/B 연결 | [`app/a_gateway.py`](../app/a_gateway.py): 실제 계획/task의 소유권·잠금·진도·자료 참조. [`app/ports.py`](../app/ports.py): 주입 가능한 경계이며 미설정 adapter는 task 사용과 위험한 삭제를 허용하지 않습니다. |
| 작업과 외부 호출 | [`app/jobs.py`](../app/jobs.py): 공통 JobService. [`app/providers.py`](../app/providers.py): 모델 allowlist·요청 제한·provider 응답 검증. [`app/key_validation.py`](../app/key_validation.py): 생성과 수명주기가 다른 키 검증 claim. |
| 파일·비밀값·실행 | `storage`는 private 파일/서명 URL, `extraction`은 제한된 별도 parser 프로세스, `credentials`는 암호화 TTL vault입니다. [`app/worker.py`](../app/worker.py)가 Job과 [`maintenance`](../app/maintenance.py)의 삭제 outbox를 처리합니다. |

공개 게시·점수 인증 테이블의 존재는 해당 비즈니스 API 구현을 의미하지 않습니다. 실험용 A Alembic 이력은 격리되어 있으며 운영 `create_schema()`와 Alembic 실행은 지원 경로가 아닙니다. DB 변경은 [migration 안내](../migrations/README.md)를 따릅니다.

## 요청에서 결과까지

```mermaid
flowchart LR
    R[요청·인증·입력 검증] --> T[멱등성·도메인 transaction]
    T --> J[Job 접수·202]
    J --> C[worker claim·lease]
    C --> P[입력 준비·상태 재검증]
    P --> D[dispatch 표시 commit]
    D --> X[transaction 밖 provider 호출]
    X --> F[lease·현재 Job 재검증]
    F --> S[도메인 결과와 Job 성공 함께 commit]
```

1. 각 API는 검증된 사용자와 요청 필드를 받고, 도메인에서 소유권·참조 관계·현재 상태를 확인합니다. 소유자 불일치는 리소스를 노출하지 않는 `404`로 처리합니다.
2. 생성 POST는 `core.idempotent` 안에서 요청 digest와 replay를 확인합니다. 도메인 변경, Job 접수, replay 저장은 같은 transaction입니다. 동일 요청은 재생하고 같은 키의 다른 요청은 충돌합니다. `202`는 결과 완료가 아닙니다.
3. JobService는 `analysis`, `questions`, `learning_explanation`, `grade`, `message`, `report` handler를 사용합니다. 요청별 AI 키는 도메인 payload에 저장하지 않고 최대 15분 TTL의 암호화 vault 참조로 연결합니다.
4. 자료 분석은 **짧은 DB snapshot → transaction 밖 파일 읽기/추출 → snapshot 재검증 → dispatch** 순서입니다. 일반 handler의 `prepare`는 제한된 SQL 입력 준비만 담당합니다. 자료 complete도 파싱 후 최종 transaction에서 상태를 다시 확인합니다.
5. worker는 provider 호출 전에 dispatch 표시를 commit합니다. 응답 후 lease/fencing token, 소유자, 리소스의 현재 Job 포인터를 다시 확인하고 결과와 Job 상태를 함께 저장합니다. 오래된 worker 결과가 후속 작업을 덮어쓰지 못하게 합니다.

키 검증은 별도 흐름입니다. 짧은 claim을 먼저 commit하고 DB 연결 없이 provider에 확인한 뒤 claim token으로 완료를 fence합니다. 생성 작업의 멱등성 transaction과 기계적으로 합치면 외부 I/O 동안 DB 연결을 보유하게 됩니다.

## 유지해야 하는 경계

- **도메인 잠금:** task 변경은 goal → plan → task 순서이며, 연결된 채점/Teach-back은 그 뒤 question/session을 잠급니다. 사전 ID 조회는 잠금 대상을 찾는 용도입니다. 잠금 후 소유권·상태를 다시 확인하고 MySQL current read를 사용합니다. worker의 Job fencing은 이 도메인 검증에 추가됩니다.
- **진도 조회와 변경:** 화면용 집계는 batch read로 줄일 수 있습니다. 재계획·완료 판단의 authoritative read는 필요한 잠금과 최신 데이터 의미를 유지해야 합니다. SQL 수를 줄이기 위해 일반 snapshot read로 대체하지 않습니다.
- **고정 버전:** 계획·문제·세션은 확인된 버전을 참조합니다. 최신 버전으로 임의 교체하거나 B 데이터를 A JSON 복제본으로 유지하지 않습니다. 입력 DTO 검사와 저장된 데이터/provider 출력의 관계 검증은 서로 다른 신뢰 경계입니다.
- **비밀값:** 검증 오류·provider 오류·SQL 예외에 요청 body와 키를 노출하지 않습니다. rollback은 새 vault 항목을 제거하고, 이전 항목의 폐기는 commit 후 수행합니다. commit 결과가 불확실하면 새 항목은 제한된 TTL 안에서 유지합니다.
- **삭제:** 자료 참조 확인과 삭제 outbox를 거칩니다. 참조 잠금 경합은 사용 중으로 취급하고, 실제 파일 삭제는 worker가 재실행 가능하게 처리합니다.
- **테스트 대역:** 인증/provider 대체는 명시적 test profile 주입으로만 허용합니다. 환경 변수만으로 가짜 provider나 인증이 선택되지 않습니다.

`outcome_unknown`은 **실행 중이 아닌 terminal 상태**이며, 외부 실행·비용의 결과가 확정되지 않았다는 뜻입니다. `queued/running`의 busy guard와 재호출 비용 동의는 별도 판단입니다. 자동 재호출하지 않고, retry는 확정된 재시도 가능 실패에만 허용합니다. restart는 새 멱등성 키와 `acknowledge_possible_duplicate_cost=true`, 현재 리소스 검증이 필요합니다. terminal Job과 세션의 `active/report_pending` 상태를 같은 개념으로 취급하지 않습니다.

## 의존성과 구조 정리

현재 A router는 canonical `app.core/models`를 직접 사용합니다. gateway와 A question router는 `app.projections`를 가져오므로 gateway → HTTP router → grading → gateway 순환 의존은 제거되었습니다. `materials/content`의 버전·채점용 읽기는 아직 A service에서도 사용합니다. 구조 정리는 **공유 조회를 router에서 독립시키고 실제 중복만 모으는 범위**로 제한했습니다.

현재 구현 위치는 아래와 같습니다. 소스 반영과 최종 전체 회귀·MySQL 검증은 별개이며, commit별 검증 상태는 [통합 검증 기록](INTEGRATION_VALIDATION.md)에서 확인합니다.

| 대상 | 현재 구현 | 보존하는 계약 |
| --- | --- | --- |
| Cursor | `pagination`이 material/content/A codec을 제공하고 A `routers/common`은 기존 이름을 재노출 | 서로 다른 기존 payload·서명 도메인·timestamp 검증·오류를 유지 |
| 요청 envelope·JSON | `core.request_envelope`를 materials/content/jobs/teachback이 사용. `core.strict_json`을 pagination/provider가 공유 | 콘텐츠의 query dict, 나머지의 raw query와 호출자별 body 직렬화를 보존. JSON 중복 키·비표준 수 거부 유지 |
| 응답 projection | `question_dtos`가 질문 children을, `attempt_dtos`가 graded evidence를 일괄 조회. gateway도 직접 사용 | 호출자가 소유권·set membership을 먼저 검사. 공개 문제에는 answer key를 노출하지 않고 graded 시도에만 결과 포함 |
| 계획 조회 | `a_gateway.task_concepts_many/task_completed_counts`를 계획 상세·Today·생성·재계획에서 공유 | 응답 순서·값, 재계획의 authoritative locking read. 일반 집계와 변경 판단의 읽기 의미를 구분 |

동일한 격리 fixture에서 계수한 SELECT 변화는 다음과 같습니다. 계획 HTTP 측정은 공통 JWT 사용자 조회를 제외하고, set DTO 측정은 gateway의 attempt 조회를 포함하지 않습니다. HTTP 지연·메모리·운영 부하 전체를 측정한 수치는 아닙니다.

| 측정 대상·크기 | 변경 전 | 변경 후 |
| --- | --- | --- |
| 계획 상세, task 3 / 300개 | 8 / 503 | 6 / 6; 6,000개도 6 |
| Today, task 3 / 300개 | 9 / 504 | 7 / 7 |
| 계획 목록, plan 1 / 30개 | 3 / 61 | 3 / 3 |
| 계획 생성 응답, task 3 / 300개 | 12 / 507 | 10 / 10 |
| 재계획, task 3 / 300개 | 20 / 1,010 | 18 / 18 |
| set DTO, question 1 / 20개 | 7 / 102 | 5 / 5 |

회귀 진입점은 `test_plan_queries_n14.py`와 `test_projections.py`입니다. 남은 조회 후보는 여러 세트에 걸친 페이지 loading, 개념 버전의 checklist/evidence, weakness별 집계, report item별 evidence입니다. 이번 변경으로 모든 N+1 조회가 제거된 것은 아닙니다.

작은 파일이라는 이유로 `key_validation`, `extraction`, `maintenance`, `teachback_prompts`를 합치지 않습니다. 각각 외부 I/O claim, parser 격리, 삭제 재시도, 고정 입력/출력 검증이라는 별도 수명주기를 갖습니다. 공통 repository나 거대한 utility 모듈을 도입하는 것도 현재 정리의 목표가 아닙니다.

## 호환 경로와 실행 경로

- `language_learning_api.main:create_app`은 문서화된 실행 alias입니다. `language_learning_api.models`와 `database.Base`는 같은 canonical ORM을 다시 노출합니다. 별도 테이블이나 DB를 만드는 위치가 아닙니다.
- A의 `auth/config` wrapper, 모델 이름 alias는 저장소 소비자가 적거나 없어도 기존 외부 A import와 구분해야 합니다. 제거 전 지원 경로를 명시하고 내부 호출을 먼저 이동합니다.
- `app.content.question_dto`, `app.materials.evidence_dto`, A `routers.questions.attempt_dto`는 `app.projections`의 함수를 재노출합니다. A `routers/common`의 cursor 이름과 `app.providers.strict_json`도 기존 import 경로를 유지합니다.
- `services/job_results.py`는 은퇴 안내문입니다. 과거 `complete_*` callback은 lease를 증명하지 못하므로 결과 저장 경로로 복원하지 않습니다.
- `AnalysisHandler.prepare/persist`는 직접 호출하는 기존 테스트를 위한 호환 경로가 있습니다. worker는 split preparation을 사용하므로 이를 지우거나 합칠 때 외부 추출을 transaction 안으로 되돌리지 않습니다.
- A `schemas.py`에는 실제 입력/응답 검증 모델과 현재 연결되지 않은 응답 DTO가 섞여 있습니다. `main`이 canonical JSON에서 등록된 경로만 골라 OpenAPI를 게시하므로 클래스 존재만으로 응답 검증이 적용되는 것은 아닙니다.

## 검증 진입점

의존성 설치는 README의 `requirements.lock` + editable install 절차를 사용합니다. 저장소 루트에서 기본 확인은 다음과 같습니다.

```sh
python -m ruff check app src tests scripts
python -m ruff format --check app src tests scripts
python -m pytest -q --ignore=tests/test_mysql.py
# TEST_MYSQL_URL이 설정된 disposable MySQL에만 실행:
python -m pytest -q tests/test_mysql.py
```

| 변경 경계 | 확인할 테스트 |
| --- | --- |
| 실제 HTTP 통합 | `tests/test_ab_e2e.py`, `test_ab_auth_cursor.py`, `test_review_n_e2e.py` |
| 작업·비밀값·외부 I/O | `test_jobs.py`, `test_job_runtime_n.py`, `test_credentials_cleanup.py`, `test_key_validation_n.py`, `test_material_storage_n.py` |
| 계획/terminal guard | `test_ab_plans.py`, `test_plan_queries_n14.py`, `test_terminal_jobs_n11.py` |
| 공유 helper·projection | `test_pagination_compat.py`, `test_projections.py`: 기존 cursor/request 형식, 응답·redaction·조회 수 |
| DDL·잠금·현재 읽기 | `test_mysql.py`와 해당 boundary helper. SQLite 성공만으로 MySQL 의미를 판단하지 않음 |

기본 테스트는 격리된 DB/storage, 합성 키, 명시적 provider 대역을 사용합니다. Windows HTTP 테스트에는 loopback과 hard-link 생성이 가능한 실행 환경이 필요합니다. CI 정의는 [backend-b.yml](../.github/workflows/backend-b.yml), 최신 실행 결과와 한계는 [통합 검증 기록](INTEGRATION_VALIDATION.md)에서 확인합니다. 구조 변경은 응답 동등성·이전 cursor/replay 호환·소유권·query 증가량으로 검증하며 테스트 개수 자체를 품질 근거로 삼지 않습니다.

## 남은 정책 결정

| 항목 | 아직 결정하지 않은 범위 |
| --- | --- |
| N10 | 개념 확정 PATCH의 멱등성 정책. POST 규칙을 자동 확대하지 않음 |
| N12 | 실패한 report와 `report_pending` 세션의 복구/이탈 정책. terminal Job 수정만으로 세션 전이를 임의 변경하지 않음 |
| N13 | unknown 자습 설명 이후 반복 생성의 중복 비용 동의·동일 요청 판정 정책. 일반 restart 계약과 별도로 합의 필요 |

이 결정들과 미구현 13개 API는 구조 정리로 완료되었다고 간주하지 않습니다.
