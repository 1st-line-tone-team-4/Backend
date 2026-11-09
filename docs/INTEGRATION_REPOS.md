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

## 다음 통합 후보

1. 문제 생성·채점·Teach-back·평가: 백엔드(OpenAI/Anthropic BYOK)와 AI repo(Groq/Gemini)가 같은 기능을 따로 구현하고 있어 어느 쪽을 쓸지 결정이 필요합니다.
2. frontend: 모든 화면이 `src/data/mock.js`를 사용합니다. `.env.example`의 기본 주소는 `http://localhost:8080/v1`인데 백엔드 실행 예시는 8000 포트입니다. 오류 형식(`error.code`, `error.message`)은 이미 일치합니다.
3. AI repo 테스트 3개는 저장소에 없는 `swagger-api-final.yaml`을 읽어 실패합니다. 계약 원본은 Backend `contracts/openapi-1.3.0.json`입니다.
