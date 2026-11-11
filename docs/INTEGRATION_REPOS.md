# 팀 repo 최초 통합 (2026-11-09)

브랜치 `integration/initial-20261109`는 `integration/a-b-20261101`(ce268e9, A+B 통합)을 기반으로 한 첫 번째 팀 전체 통합 브랜치입니다.

## 통합 시점의 브랜치 현황

| repo | 브랜치 | 커밋 | 통합 여부 |
| --- | --- | --- | --- |
| Backend | `integration/a-b-20261101` | ce268e9 | 기반 |
| Backend | `feature/a-part-api` | 7d237ae | 이미 기반에 포함 |
| Backend | `feature/b-content-pipeline-20261101` (기본 브랜치) | 4e6752f | 이미 기반에 포함 |
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

## 다음 통합 후보

1. 문제 생성·채점·Teach-back·평가: 백엔드(OpenAI/Anthropic BYOK)와 AI repo(현재 Gemini)가 같은 기능을 따로 구현하고 있습니다. AI repo는 11/12에 백엔드 연동용 내부 명세 `ai-internal-api.yaml`을 추가했습니다.
2. AI repo 테스트 일부는 저장소에 없는 `swagger-api-final.yaml`을 읽으며 파일이 없으면 skip합니다. 계약 원본은 Backend `contracts/openapi-1.3.0.json`입니다.
