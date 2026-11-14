# 통합 API 안내 — 현재 제공 범위

이 문서는 `app.main:create_app`에 실제 등록된 **41개 경로 / 52개 작업(A+B)**의 연결 안내입니다. 경로 앞에는 `/v1`을 붙입니다. 구현된 최종 명세는 [월 구독형 OpenAPI YAML](../contracts/openapi-final-monthly-subscription.yaml)이며, 전체 계약 원본은 [월 구독형 OpenAPI JSON](../contracts/openapi-1.4.0-monthly-subscription.json)입니다. 공개·점수 API와 checkout·구독 권한 검증은 아직 구현 범위에 없습니다.

실행 중인 `/docs`와 `/openapi.json`은 등록된 계약 경로만 게시합니다. 파일 형태의 [openapi-full.json](openapi-full.json), [openapi-a.json](openapi-a.json)은 배포하려는 commit에서 갱신된 snapshot인지 확인해야 합니다. 요청 body·필드 제약·응답 schema의 기준은 OpenAPI이고, 이 문서의 표는 경로 요약입니다.

## 공통 동작

| 항목 | 규칙 |
| --- | --- |
| 인증 | 외부 제공자의 RS256 JWT; `Authorization: Bearer <token>` |
| 사용자 생성 | 유효한 첫 `(issuer,subject)` 인증 시 로컬 사용자 생성; 비밀번호 가입·로그인·토큰 발급 API 없음 |
| 멱등성 | POST의 `Idempotency-Key` 16–128자; 같은 키와 다른 요청은 `409` |
| AI 요청 | 허용된 `model_id`; 제공자 키는 서버 Secret Manager에서 관리하고 클라이언트 요청에는 포함하지 않음 |
| 구독 | `GET /billing/capabilities`(사용자별 `pricing_pending`/`active`), 데모용 `PUT /billing/subscription` `{active}`; 가격·결제 연동 전 checkout 비활성 |
| 성공·목록 | `{data:…}` / `{data:[…],next_cursor:…}` |
| 오류 | `{error:{code,message,request_id,details}}`; 소유자 불일치 `404`, 입력 오류 `422`, 상태 충돌 `409` |
| 날짜·ID | Asia/Seoul 달력 날짜, UTC timestamp, UUID 문자열 |
| 비동기 | `202`는 접수만 의미; Job 조회 후 결과 리소스 확인 |

목록의 cursor는 해당 소유자와 필터에 맞게 사용합니다. 클라이언트는 서버가 반환한 cursor를 그대로 전달합니다. PATCH에 POST의 멱등성 요구를 임의로 적용하지 않습니다.

## 현재 경로

### 사용자·목표·계획

| 메서드 | 경로 |
| --- | --- |
| GET | `/me`, `/today` |
| GET, POST | `/exam-goals` |
| PATCH | `/exam-goals/{goalId}` |
| GET, POST | `/study-plans` |
| GET | `/study-plans/{planId}` |
| POST | `/study-plans/{planId}/reschedule` |
| PATCH | `/plan-tasks/{taskId}` |

계획은 준비된 비보관 자료의 현재 확정 개념 버전을 고정합니다. 목표별 활성 계획은 하나이며 날짜 수정은 기존 계획을 자동 덮어쓰지 않습니다. task 표시 정보·완료량은 실제 관계와 학습 결과에서 계산합니다. 상세 흐름은 [A API 안내](A_API_SPEC.md)를 참고합니다.

### 자료·개념·근거

| 메서드 | 경로 |
| --- | --- |
| GET, POST | `/materials` |
| GET, PATCH, DELETE | `/materials/{materialId}` |
| POST | `/materials/{materialId}/complete` |
| GET | `/materials/{materialId}/units` |
| GET, PATCH | `/materials/{materialId}/concepts` |
| GET | `/materials/{materialId}/concepts/{conceptId}/versions` |
| GET | `/materials/{materialId}/source` |
| GET | `/materials/{materialId}/chunks/{chunkId}` |

업로드 생성 응답의 서명된 PUT URL로 파일을 올린 다음 complete를 호출합니다. 분석 완료와 개념 확정은 별도 단계입니다. 확정 버전·checklist·근거는 이후 계획·문제·세션에서 그대로 참조됩니다. 보관은 이력을 유지하고, 삭제는 참조 검사 후 outbox로 처리합니다.

파일 형식은 PDF/TXT/PNG/JPEG, 일반 업로드 상한은 20MiB/PDF 200페이지입니다. 파서·문자량·픽셀·provider별 추가 한도가 있으므로 이 상한 안의 모든 파일이 처리 가능하다는 보장은 없습니다. 소스 접근에는 별도의 서명 URL 검증이 적용됩니다.

### 문제·채점·자습 설명

| 메서드 | 경로 |
| --- | --- |
| GET, POST | `/question-sets` |
| GET | `/question-sets/{setId}`, `/questions/{questionId}` |
| GET, POST | `/questions/{questionId}/attempts` |
| GET | `/attempts/{attemptId}` |
| GET, POST | `/learning-explanations` |
| GET | `/learning-explanations/{explanationId}` |

task와 연결한 문제 생성은 소유자·활성 revision·단원·정확한 고정 버전 집합·남은 목표량을 검증합니다. 문제 DTO에는 답안 키가 없고, 채점 결과는 완료된 개인 시도에서 읽습니다. 자습 설명은 별도의 불변 성공 리소스이며 task 진행·Teach-back coverage를 변경하지 않습니다.

AI 생성 API는 서비스 관리 제공자 키를 사용합니다. 결과가 불확실한 Job의 restart는 `acknowledge_possible_duplicate_execution=true`를 요구합니다. 이 동의는 중복 AI 호출 가능성에 대한 것이며 사용자에게 별도 결제를 청구한다는 뜻은 아닙니다. 실제 결제와 구독 접근 권한 검증은 후속 작업입니다.

### Teach-back·기록

| 메서드 | 경로 |
| --- | --- |
| POST | `/teachback-sessions` |
| GET, PATCH | `/teachback-sessions/{sessionId}` |
| GET, POST | `/teachback-sessions/{sessionId}/messages` |
| POST | `/teachback-sessions/{sessionId}/final-answer` |
| POST | `/teachback-sessions/{sessionId}/complete` |
| GET | `/teachback-sessions/{sessionId}/report` |
| POST | `/teachback-sessions/{sessionId}/feedback` |
| GET | `/records/sessions`, `/records/weaknesses`, `/records/actions`, `/records/public-attempts` |
| PATCH | `/records/actions/{actionId}` |

세션은 고정 개념 버전을 기준으로 진행합니다. `voice_transcript`는 이미 텍스트로 변환된 입력이며 음성 인식 기능 자체를 의미하지 않습니다. 리포트의 전체 checklist 평가와 evidence 관계를 검증하고 coverage를 서버에서 계산합니다.

### AI·공통 Job

| 메서드 | 경로 |
| --- | --- |
| GET | `/ai/models` |
| GET | `/jobs/{jobId}` |
| POST | `/jobs/{jobId}/retry`, `/jobs/{jobId}/restart` |
| GET | `/billing/capabilities` |

활성 모델 기본값은 비어 있습니다. 서버 설정에 provider 키가 없는 모델은 `available=false`이며 생성 요청을 거부합니다. 같은 worker가 분석·문제·자습 설명·채점·메시지·리포트를 처리합니다.

## 상태 구분

| 리소스 | 상태 |
| --- | --- |
| Job | `queued`, `running`, `succeeded`, `failed`, `outcome_unknown` |
| 자료 | `upload_pending`, `queued`, `processing`, `ready`, `failed` |
| 학습 계획 | `active`, `superseded` |
| task | `pending`, `in_progress`, `completed`, `skipped` |
| 문제 세트 | `generating`, `ready`, `failed` |
| 채점 시도 | `grading`, `graded`, `failed` |
| 자습 설명 | `generating`, `ready`, `failed`, `outcome_unknown` |
| Teach-back 세션 | `active`, `report_pending`, `completed`, `abandoned` |

Job의 `outcome_unknown`은 provider가 실행했는지 확정할 수 없다는 뜻입니다. 자동 재요청하지 않습니다. retry는 현재 Job의 확정된 재시도 가능 실패에 한정되고, restart는 `acknowledge_possible_duplicate_execution=true`를 요구합니다. 완료된 과거 Job으로 현재 리소스나 새 시도를 덮어쓰지 않습니다.

## 계약 밖의 운영 경로와 제외 범위

`GET /health`는 프로세스 응답 확인용입니다. `/v1/private-storage/{materialId}?token=…`의 PUT/GET은 목적·소유자·만료를 검증하는 내부 파일 adapter 경로이며 일반 학습 계약 목록과 구분됩니다.

공개 게시·공개 문제 풀이 생성·점수 인증/관리자 심사·결제 API는 이번 통합 서버에 등록되지 않습니다. `publications`, `public_attempts`, `score_claims` 테이블은 전체 계약의 FK와 개인 이력 구조를 맞추기 위해 존재하며, 존재 자체가 사업 기능 구현을 의미하지 않습니다. `/records/public-attempts`는 이미 저장된 개인 이력을 조회합니다.

설치·인증 제공자·현재 프런트엔드 상태·CORS 연결 조건은 [README](../README.md), 기존 DB 업그레이드 제한은 [마이그레이션 안내](../migrations/README.md), 실행 증거와 남은 검증 범위는 [검증 기록](VALIDATION.md)을 확인합니다.
