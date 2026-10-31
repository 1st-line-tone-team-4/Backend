# Team4 Backend B

FastAPI/Python 3.12 기반 B 구현입니다. 사용자가 FastAPI 방향을 확인했습니다. 기준은 [메인 Notion](https://app.notion.com/p/40a5df12680381b58e59e5858da561e6)의 계약 `1.3.0-backend-proposal`입니다. A 기능과 운영 배포는 포함하지 않습니다.

## 실행과 검증

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
```

Linux/macOS에서는 `.venv/bin/python`을 사용합니다. 테스트는 임시 SQLite, private 파일 저장소, 테스트 전용 인증·AI·A gateway를 사용합니다. `tests/test_e2e.py`는 실제 loopback HTTP와 별도 worker 프로세스까지 실행합니다. 로컬 보안 샌드박스가 loopback을 차단하면 해당 실행에만 네트워크 권한이 필요합니다.

설정은 `.env.example`의 변수명을 참고해 프로세스 환경/secret manager로 공급합니다. `.env` 자동 로딩이나 기본 운영 비밀키는 없습니다. API 키는 환경 변수에 설정하지 않고 각 요청의 `X-AI-Key`로 전달합니다. 사용자 등록·로그인은 A가 제공하며 `users(auth_issuer,auth_subject)`가 검증된 JWT와 매핑되어야 합니다.

```sh
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
python -m app.worker
# 한 작업과 due 삭제 outbox만 처리
python -m app.worker --once
```

운영은 MySQL 설정, RS256 public key/issuer/audience, HTTPS, 외부에서 공급한 서명키와 AES 키가 필요합니다. 리버스 프록시의 전달 헤더는 신뢰하는 프록시 주소만 허용하세요. 프록시/HTTP client 로그에도 Authorization, X-AI-Key, signed URL query를 기록하지 않아야 합니다. `.env.example`는 실제 비밀값이 아닙니다. `PROFILE=test`만으로 인증 대역이 켜지지 않으며 대역은 테스트 코드가 명시적으로 주입합니다.

API와 worker는 같은 MySQL, private storage root, AES 키를 사용합니다. 키는 별도 SQLite vault에 AES-256-GCM 암호문으로 최대 15분만 보관하고 terminal 작업에서 폐기합니다. 현재 vault는 같은 호스트/공유 private 파일 경로를 전제로 하므로 여러 호스트 운영에는 별도 TTL secret-store adapter가 필요합니다. OS 파일 권한은 운영자가 service identity 전용으로 설정해야 합니다.

## 구현 API

모든 경로 앞에 `/v1`이 붙습니다. 생성 POST에는 `Idempotency-Key`, AI POST에는 `X-AI-Key`가 필요합니다. `/docs`와 `/openapi.json`에는 B 계약만 게시합니다.

| 영역 | 경로 |
| --- | --- |
| 자료 | `POST/GET /materials`, `GET/PATCH/DELETE /materials/{materialId}`, `POST /materials/{materialId}/complete` |
| 근거·단원 | `GET /materials/{materialId}/units`, `/source?page=1`, `/chunks/{chunkId}` |
| 개념·버전 | `GET/PATCH /materials/{materialId}/concepts`, `GET /materials/{materialId}/concepts/{conceptId}/versions` |
| 문제 | `POST/GET /question-sets`, `GET /question-sets/{setId}`, `GET /questions/{questionId}` |
| 자습 설명 | `POST/GET /learning-explanations`, `GET /learning-explanations/{explanationId}` |
| AI·Job | `GET /ai/models`, `POST /ai/validate-key`, `GET /jobs/{jobId}`, `POST /jobs/{jobId}/retry`, `/restart` |

추가 구현인 `PUT/GET /private-storage/{materialId}?token=...`는 목적·사용자·자료·만료가 서명된 private 파일 전송 adapter입니다. 원본 경로 입력이나 임의 URL fetch는 받지 않습니다. 자료 삭제는 DB transaction에서 outbox를 기록하며 worker가 즉시 제거를 시도하고 실패 시 backoff 재시도합니다. worker 중단/저장장치 장애가 지속되면 24시간 제거 목표를 보장할 수 없으므로 운영 모니터링이 필요합니다.

## A 통합 시작점

1. [분담 계약](https://app.notion.com/p/40a5df12680381189fabfcaa690d1b9f)과 [migration 경계](migrations/README.md)를 먼저 확인합니다. A 소유 `users`, `plan_tasks`를 B migration이 생성하지 않습니다. 테스트의 id-only `PlanTask`는 운영 A 모델이 아닙니다.
2. A JWT 공급자와 `JWTAuthenticator`를 연결합니다. 기존 인증 시스템이 다른 검증 방식을 쓰면 같은 `authenticate(request)->user_id` 경계를 맞춥니다. 테스트용 헤더 인증을 운영으로 옮기지 않습니다.
3. `TaskGateway.read_question_task(session, user_id, task_id)`를 구현해 `QuestionTaskState`를 반환하고 `build_context(..., task_gateway=...)`에 전달합니다. A adapter는 같은 transaction에서 소유 plan과 task 행을 잠그고 활성 revision, 소유자, 단원, 고정 버전 집합, task 유형, 목표량과 실제 채점 완료량을 읽습니다. A의 plan 전환·진행량 쓰기도 같은 잠금과 순서를 사용해야 합니다. 공통 `validate_question_task(..., count=...)`가 소유권·활성 revision·정확한 버전 집합·`solve` 유형·`count <= target_count - completed_count`를 검사합니다. 생성은 완료량을 늘리지 않습니다. `material_in_use`, `read_question_attempts`도 A의 owner/참조/set 검사로 구현합니다. 기본 gateway는 task 연결과 자료 삭제를 보수적으로 거부하며, 빈 attempt 결과는 독립 B에 알려진 시도가 없다는 의미입니다.
4. A는 `materials.get_confirmed_version`과 `content.get_grading_question`을 내부에서 읽습니다. 후자는 정답/채점 근거를 포함하므로 사용자 응답으로 직접 노출하지 않습니다. 공개 문제 GET은 정답을 제외합니다.
5. A의 `grade/message/report` handler를 공통 JobService에 등록할 수 있습니다. A handler가 자신의 owner/latest-pointer/state lock, `prepare/persist/set_state/is_current/lock_resource`를 구현해야 합니다. B가 A의 프롬프트·채점·보고서 저장을 대신하지 않습니다.

자습 설명 생성·열람은 task 완료량이나 coverage를 변경하지 않습니다. 개념 확정은 append-only이며 이전 문제/설명은 생성 당시 버전과 근거를 유지합니다. task에 고정된 과거 confirmed 버전의 문제 생성과 과거 confirmed 버전의 자습 설명도 허용합니다. task가 없는 새 문제세트는 현재 confirmed 버전을 사용합니다.

## Job과 실패 복구

worker는 DB 조건부 claim, lease/fencing, 활성 resource/fingerprint 유일 제약을 사용합니다. 외부 호출 전에 dispatch를 영속 기록합니다. 그 뒤 crash/timeout이면 `outcome_unknown`으로 남기고 자동 재호출하지 않습니다. retry는 확정된 호출 전/인증/요청제한 실패만 최대 3회, restart는 새 멱등키와 JSON boolean `acknowledge_possible_duplicate_cost=true`가 필요합니다. 외부 provider의 exactly-once는 보장하지 않습니다.

같은 입력의 문제세트를 새로 생성할 때도 기존 `outcome_unknown` 작업이 있으면 매 요청마다 위 동의가 필요합니다. successor의 성공·실패는 이전 호출의 과금 여부를 확정하지 않으므로 경고를 해제하지 않습니다. 같은 입력의 queued/running 작업이 있으면 동의 여부와 무관하게 `JOB_IN_PROGRESS`를 우선 반환합니다.

공개 취소 API는 원계약에 없어 추가하지 않았습니다. 내부 `JobService.cancel`은 queued 작업을 실패 처리하고 진행 중 외부 호출은 결과 불명으로 남겨 늦은 쓰기를 차단합니다. 성공 결과는 불변입니다.

## 검증 경계와 구현 차이

- 실제 OpenAI 및 Anthropic fixed-endpoint 어댑터가 있으며 allowlist의 `openai:<model>` 또는 `anthropic:<model>`만 호출합니다. 기본 활성 모델은 없습니다. Anthropic은 공식 Messages API `2026-10-21`과 `x-api-key` 인증을 사용합니다. [공식 문서](https://platform.claude.com/docs/en/manage-claude/authentication)는 API 키의 Bearer 인증도 지원하므로 이전 Bearer 방식이 반드시 401을 유발했다는 주장은 하지 않습니다. 모델의 JSON/multimodal 지원과 실제 과금·품질은 live 확인 전까지 미검증입니다. `GET /models/{model}` 키 검사는 생성을 호출하지 않습니다. 생성은 엄격한 JSON 파싱과 도메인 검증을 통과해야 하며 provider의 constrained-schema mode를 사용한다고 주장하지 않습니다.
- TXT는 UTF-8, PDF는 pypdf, PNG/JPEG는 Pillow 검증 후 실제 image data를 provider에 전달합니다. 이미지 OCR은 AI가 반환한 전사문을 근거로 쓰고 사람의 개념 확정을 요구합니다. substring/ID 검증은 OCR의 의미 정확성을 보증하지 않습니다. 텍스트 없는 스캔 PDF는 현재 거부됩니다.
- 원계약 20MiB/200페이지 외에 파서 안전 한도를 추가했습니다: 추출 UTF-8 120,000 bytes, 이미지 40M pixels, 파서 subprocess 20초·프로세스당 동시 2개, PDF 개별 압축해제 8MiB. 초과는 422, 처리 슬롯 부족은 503입니다. POSIX 메모리 1GiB 제한, Windows 768MiB 감시는 best effort이므로 운영 OS/container 제한도 필요합니다. 유효한 대형 파일도 거부될 수 있으며 모든 모델의 context 크기에 맞는다고 보장하지 않습니다.
- provider 응답 2MiB, completion 최대 16,384 tokens, 총 요청 제한 시간을 적용합니다. 모델별 지원/더 작은 context는 운영 allowlist 설정 시 확인해야 합니다.
- SQLite 개발 모델과 MySQL migration에 `dispatched_at`, `credential_ref`, `last_polled_at`, `active_resource_id`를 추가했습니다. 원 SQL과 차이는 migration 문서에 기록했습니다. 원 계약 전체 DDL은 참고본이며 자동 실행하지 않습니다.
- 분석 청크 ID는 같은 job의 재시도에서 안정적으로 재사용하기 위해 job UUID로부터 UUIDv5로 유도합니다. 다른 리소스 ID는 UUIDv4입니다. OpenAPI의 `format: uuid`에는 맞지만 서술형 계약의 일괄 UUIDv4 기본값과는 다른 내부 선택입니다.
- `.github/workflows/backend-b.yml`은 Linux SQLite 회귀 및 새 MySQL 8.0 service에서 migration+HTTP E2E를 실행합니다. 최초 구현 SHA `259675314c0cf2bb7e4d7120e4206d4907451578`의 [CI에서 두 경로 모두 통과](https://github.com/1st-line-tone-team-4/Backend/actions/runs/41216673636)했습니다. 이후 변경은 해당 SHA의 CI 결과로 확인합니다. `TEST_MYSQL_URL`이 없으면 로컬 MySQL 테스트는 명시적으로 skip됩니다. 실제 A 스키마/운영 DB에 적용한 검증은 아닙니다.

## 승인된 실제 모델 시험 도구

`scripts/live_anthropic_e2e.py`는 별도 승인된 합성 자료 시험 전용입니다. 기본 pytest/CI에서 실행되지 않습니다. 직접 Anthropic API 키로 확인된 지정 파일만 읽고, 실제 키는 메모리 adapter로 처리해 vault/DB/로그/Git에 저장하지 않습니다. 파일 자체를 이동/삭제하지 않습니다. 입력은 UTF-8(BOM 포함), BOM이 있는 UTF-16, ASCII 바이트 패턴이 명확한 BOM 없는 UTF-16과 단일 `ANTHROPIC_API_KEY` 라벨/일치하는 따옴표를 지원합니다. 여러 후보나 내부 공백은 거절하며, 고정 prefix만으로 유효성을 단정하지 않습니다. 장부 잠금·연결·메모리 자격증명은 실패 경로에서도 정리합니다.

선정 모델은 `claude-haiku-5-5`, 출력·추론 합계 상한은 호출당 4,096 tokens입니다. [공식 가격](https://platform.claude.com/docs/en/models/haiku-5-5/overview)의 더 비싼 구간으로 전체 1M 입력 context까지 예약하여 호출당 최대 `$0.51024`, 공통 ledger 8회 상한 `$4.08192`를 적용합니다. 모든 실행에 **같은 ledger**를 사용해야 하며 총 승인 예산 `$10`을 초과할 수 없습니다. 예약은 환급하지 않고 결과불명 시 중단합니다. 실제 usage의 input/output을 별도 기록하며 output에는 reasoning이 포함되어 별도 분해 수치로 추정하지 않습니다. 현재 모델·가격 검증일은 2026-10-31입니다.

실제 사용 전 키의 제공업체/전송 대상, 모델/가격/예산과 합성 데이터 범위를 확인해야 합니다. 연결 실패·키 형식 불일치·결과불명에 자동 재시도하지 않습니다. `scripts/live_provider_smoke.py`는 별도의 사용자 직접 실행용 OpenAI 단일 호출 도구이며 기본 동작은 네트워크 없는 dry-run입니다.

## 계약 출처

`contracts/openapi-1.3.0.json`과 `schema-proposal-1.3.0.sql`은 2026-10-31 메인 Notion의 [전체 OpenAPI](https://app.notion.com/p/40a5df12680381d78000ea80e0c224be), [전체 SQL](https://app.notion.com/p/40a5df126803810ca5acd255c670f44f) 코드블록을 읽은 snapshot입니다. Library 원본 v3와 동일하다는 페이지 설명 및 검토 보고서를 확인했습니다. Library 공식 materialization은 Windows `os.setxattr` 미지원으로 실패했으므로 Library에서 로컬 materialize 성공했다고 주장하지 않습니다.
