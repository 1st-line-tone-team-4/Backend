# 팀 repo 최초 통합 (2026-11-09)

브랜치 `integration/initial-20261109`는 `integration/a-b-20261101`(ce268e9, A+B 통합)을 기반으로 한 첫 번째 팀 전체 통합 브랜치입니다.

## 통합 시점의 브랜치 현황

| repo | 브랜치 | 커밋 | 통합 여부 |
| --- | --- | --- | --- |
| Backend | `integration/a-b-20261101` | ce268e9 | 기반 |
| Backend | `feature/a-part-api` | 7d237ae | 이미 기반에 포함 |
| Backend | `feature/b-content-pipeline-20261101` (기본 브랜치) | 4e6752f | 이미 기반에 포함 |
| Backend | `claude/b-review-fixes` (PR #1, closed) | b3406a1 | 제외 (병합 보류 결정) |
| AI | `main` | f1a0874 | 별도 서버로 유지, HTTP로 연동 |
| frontend | `main` | 4cb5de5 | 미연동 (화면은 mock 데이터 사용) |

## AI 서버 연동 1단계: 문서 인식

`AI_SERVICE_URL`과 `AI_SERVICE_API_KEY`를 설정하면 분석 worker가 PDF/PNG/JPEG 원본 bytes를 AI 서버 `POST /internal/materials/extract`로 보내고, 응답의 `document.pages` 텍스트로 분석 청크를 만듭니다. 청크 ID는 계속 백엔드가 job ID에서 파생합니다.

- 텍스트 레이어가 없는 스캔 PDF도 업로드 완료 단계에서 허용되고, 텍스트는 AI 서버 OCR이 공급합니다.
- 이미지는 vision 입력 대신 인식된 텍스트 청크로 분석되며, provider 이미지 preflight를 건너뜁니다.
- `text/plain`은 기존 로컬 경로를 그대로 사용합니다.
- 설정이 없으면 동작은 이전과 같습니다.

### 실패 처리

AI 서버는 자체 캐시와 `409 DOCUMENT_PROCESSING_STATE`로 중복 과금을 막으므로 백엔드 client는 자동 재시도하지 않습니다. 오류 코드는 `ai_jobs.error_code` CHECK 집합 안에서만 사용합니다.

| AI 서버 응답 | job 결과 |
| --- | --- |
| 타임아웃, 409, 504 | `outcome_unknown` / `TIMEOUT_UNKNOWN` (Upstage 처리·과금 여부 불명) |
| 429 | `failed` / `PROVIDER_RATE_LIMIT`, 재시도 가능 |
| 502, 503, 연결 실패 | `failed` / `PROVIDER_UNAVAILABLE`, 재시도 가능 |
| 401/403, 기타 | `failed` / `PROVIDER_UNAVAILABLE` (서버 간 키 설정 확인) |
| 422, `ready_for_analysis=false`, 빈 텍스트 | `failed` / `INVALID_OUTPUT` |

### 알려진 제한

- 인식은 worker 준비 단계(`prepare_seconds=60`)에서 실행되고 client 타임아웃은 50초입니다. AI 서버의 Upstage 타임아웃 기본값은 120초라서, 큰 PDF는 타임아웃으로 `outcome_unknown`이 될 수 있습니다. 대용량 자료는 Job 구조 조정이 필요합니다.
- 로컬 120,000바이트 텍스트 상한은 그대로 적용됩니다.

## 프런트엔드 연동 (2026-11-10 작업, 2026-11-12 검증)

브랜치 `integration/latest-ui-20261110`(Backend·frontend 동일 이름)은 frontend `main` 5de8d3d(11/9, 음성 채팅·학습 화면 추가)를 이 백엔드에 실제로 연결한 결과입니다. 11/10 로컬 작업을 11/12에 다시 검증해 push했습니다.

- frontend 39개 API wrapper의 경로·메서드는 구현과 모두 일치했습니다. 연결 작업은 `model_id`·`X-AI-Key` 전달, `voice_transcript` 입력 모드, Job polling 2초·`Retry-After` 준수, POST 멱등키 유지, 세션 전환 시 캐시·요청 격리, 업로드 결과 불명 복구, 실제 문제·결과·근거·리포트 화면 연결입니다. 공개 계약은 바꾸지 않았습니다.
- 백엔드 변경: 서명 업로드 PUT이 AI 문서 인식이 설정된 경우 텍스트 없는 PDF를 complete 단계와 같은 기준으로 받도록 맞춤(07779a6), AI 인식 이미지의 분석 결과 저장 오류 수정, 선택적 `CORS_ALLOWED_ORIGINS`.
- 검증(11/12, Windows): Backend 전체 테스트 통과(MySQL 테스트 1개는 disposable DB가 없어 skip), frontend 단위 테스트 47개·build 통과, 실제 화면 E2E(`npm run test:browser`) 통과. E2E는 실제 백엔드·JWT 검증·임시 SQLite와 결정적 합성 AI provider를 사용하며 외부 AI·실제 인증 제공자는 호출하지 않습니다.
- 실행: frontend `npm run dev`는 `/v1`을 `BACKEND_PROXY_TARGET`(기본 `http://127.0.0.1:8000`)으로 프록시합니다. 백엔드는 README의 `uvicorn`과 `python -m app.worker`를 함께 실행합니다.

### 남은 결정과 한계

1. **로그인**: 서버는 외부 RS256 JWT만 검증하고 로그인·토큰 발급 API가 없습니다. frontend는 실제 로그인 대신 개발용 테스트 토큰 입력(`VITE_ENABLE_TEST_LOGIN=true`, 개발 빌드 전용)만 제공합니다. 인증 제공자 선택이 데모 전 필수입니다.
2. 미구현 공개 계약 13개(publications·score-claims·billing)와 프로필 수정·비밀번호·탈퇴·알림 설정 API가 없습니다. 해당 화면은 미지원 안내를 표시합니다.
3. 실제 마이크·STT·TTS, 운영 배포(프록시/CORS), 실제 AI 품질·비용은 검증하지 않았습니다.

## AI 서버 연동 2단계: 생성·채점·평가 (2026-11-12)

AI repo의 내부 명세 `ai-internal-api.yaml`(0.3.0, Gemini)을 기준으로, `AI_SERVICE_MODELS`에 넣은 `gemini:<model>`로 요청한 AI 작업을 AI 서버가 처리합니다. 공개 API 계약은 그대로이고, 기존 OpenAI/Anthropic 모델(`ENABLED_MODELS`)의 동작도 바뀌지 않습니다. 구현은 `app/ai_generation.py`(요청·응답 변환, 오류 매핑)와 `app/ai_context.py`(DB → `StudyContext`)입니다.

| Job 종류 | AI 서버 경로 | 변환 |
| --- | --- | --- |
| `analysis` | `POST /internal/materials/analyze` | 청크에 자료 ID·이름을 붙여 보내고, `DraftAnalysis`의 근거를 `{chunk_id, quote}`로 바꿉니다. 원문과 글자 그대로 일치하지 않는 근거는 공백·대소문자 차이만 원문 기준으로 복원하고, 그래도 없으면 그 개념을 저장하지 않습니다. |
| `questions` | `POST /internal/questions/generate` | 확정 버전과 근거 원문(`StudyContext`)을 보냅니다. AI 서버는 5~20문항만 받으므로 1~4문항 요청은 5문항을 생성해 앞에서부터 사용합니다. |
| `learning_explanation` | `POST /internal/explanations/generate` | `[근거: id]` 인용의 청크 ID를 백엔드 ID로 되돌리고, 근거가 남지 않은 인용은 지웁니다. |
| `grade` | `POST /internal/questions/grade` | 저장된 정답 키를 그대로 보내고, 결과의 `correct_answer`는 백엔드의 정답 키를 사용합니다. 객관식·정확 일치는 AI 호출 없이 채점됩니다. |
| `report` | `POST /internal/evaluations/generate` | 세션에 고정된 근거 발췌만 원문으로 보냅니다. 근거가 남지 않은 `explained/partial/error`는 AI 서버 규칙과 같이 `unverified`로 바꾸고, 설명 1200자·다음 행동 2000자로 자릅니다. 점수는 기존처럼 백엔드가 다시 계산합니다. |
| `message` | (AI 서버에 텍스트 질문 경로 없음) | AI 서버는 Teach-back을 음성 전용으로 바꿨기 때문에, 텍스트 Teach-back 질문은 같은 BYOK 키로 Gemini `generateContent`를 직접 호출하는 임시 경로입니다. AI 서버에 텍스트 응답 경로가 생기면 옮겨야 합니다. |

- AI 서버는 UUID v4만 받는데 백엔드 청크 ID는 분석 Job에서 파생한 UUID v5입니다. 요청마다 v4 형태로 바꿔 보내고 응답에서 원래 ID로 되돌립니다. DB에 저장되는 ID는 바뀌지 않습니다.
- 인증: `X-AI-API-Key`(서버 간 키)와 사용자의 `X-AI-Key`(BYOK)를 함께 보냅니다. 공개 계약대로 사용자는 계속 키를 입력해야 합니다.
- 자동 재시도는 없습니다. 연결 실패는 `PROVIDER_UNAVAILABLE`(재시도 가능), 429는 `PROVIDER_RATE_LIMIT`, AI 서버의 `PROVIDER_AUTH`는 `PROVIDER_AUTH`, `INVALID_OUTPUT`은 `INVALID_OUTPUT`, 공급자 5xx·504·응답 시간 초과는 과금 여부를 알 수 없어 `outcome_unknown`(`TIMEOUT_UNKNOWN`)입니다. 요청 본문은 AI 서버 한도(2 MiB)를 넘으면 보내기 전에 실패합니다.
- 분석은 최대 12회 분할 호출(`max_calls`)로 보냅니다. 큰 자료는 AI 서버가 422로 거절할 수 있습니다. 생성 요청 하나의 대기 한도는 290초입니다.

### 검증

- `tests/test_ai_generation.py`: 서명 JWT → HTTP → DB → worker 전체 여정(`test_ab_e2e`와 같은 흐름)을 이 어댑터와 AI 서버 응답 형식의 가짜 서버로 실행해 모든 결과가 저장되는지 확인하고, 오류 매핑·ID 변환·근거 복원·문항 수·크기 제한·키 검증을 따로 확인합니다.
- 교차 검증(11/12, 저장소에는 넣지 않은 일회성 스크립트): 실제 AI repo(`d1e29b2`) 서버를 띄우고 Gemini SDK만 막은 상태에서, 위 전체 여정이 만든 요청을 모두 실제 AI 서버로 보냈습니다. 분석·문제·설명·리포트와 의미 채점 요청은 모두 AI 서버의 입력 검증을 통과해 공급자 호출 단계까지 갔고(422 없음), 정확 일치 채점은 실제 200 응답을 변환해 저장 형식과 맞는지 확인했습니다.
- 실제 Gemini 호출, 응답 품질, 비용은 검증하지 않았습니다.

### 남은 결정

1. 텍스트 Teach-back 질문을 AI 서버에서 제공할지(현재 백엔드의 임시 직접 호출), 음성 Teach-back(AI 서버 WebSocket)을 공개 API에 어떻게 노출할지.
2. 학습 계획 생성(`/internal/study-plan/generate`), 혼합 난이도, 상세 평가는 공개 계약에 없어 연결하지 않았습니다.
3. 서버 키 사용(`AI_USE_SERVER_TEXT_KEY`)으로 사용자 키 없이 운영할지는 구독 정책과 함께 정해야 합니다. 지금은 BYOK 계약을 유지합니다.

## 다음 통합 후보

1. 로그인(인증 제공자) 결정과 연결.
2. AI repo 테스트 일부는 저장소에 없는 `swagger-api-final.yaml`을 읽으며 파일이 없으면 skip합니다. 계약 원본은 Backend `contracts/openapi-1.3.0.json`입니다.
