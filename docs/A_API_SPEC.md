# A 학습 API — 통합 서버 안내

A 라우터는 `app.main:create_app`에 B 라우터와 함께 등록됩니다. 동일한 인증, `app.models.Base`, DB transaction, 멱등성, JobService, worker를 사용합니다. 설치·환경 설정·마이그레이션은 [루트 README](../README.md)를 따릅니다. A 단독 DB/서버를 실행하거나 별도의 worker 콜백으로 결과를 쓰지 않습니다.

정확한 요청·응답 schema는 실행 중인 `/openapi.json`과 [구현된 최종 OpenAPI 명세](../contracts/openapi-final-monthly-subscription.yaml)를 참고합니다. 전체 계약과 구현되지 않은 공개·점수 API 구분은 [통합 경로 목록](API_SPEC_FULL.md)에서 확인합니다.

## 공통 규칙

- 경로 접두사는 `/v1`입니다. 외부 RS256 JWT를 Bearer 헤더로 전달합니다. 첫 유효 인증에서 로컬 사용자를 생성하며, 비밀번호 가입·로그인과 토큰 발급은 외부 인증 제공자의 책임입니다.
- POST는 `Idempotency-Key`가 필요합니다. AI 작업 POST는 허용된 `model_id`를 받으며, 제공자 키는 서버에서 공급합니다. 클라이언트는 AI 키를 전송하지 않습니다.
- 성공 응답은 `{data:…}`, 목록은 `{data:[…],next_cursor:…}`입니다. 실패는 `{error:{code,message,request_id,details}}`입니다.
- 날짜는 Asia/Seoul, timestamp는 UTC입니다. ID는 UUID 문자열입니다. 다른 사용자의 리소스는 `404`로 처리합니다.
- A 목록 cursor는 소유자·필터에 묶인 서명 값입니다. 다른 사용자나 다른 필터에 재사용하지 않습니다.

## 목표·계획·오늘 학습

| 메서드 | 경로 | 입력·결과 |
| --- | --- | --- |
| GET | `/me` | 사용자 프로필 |
| GET / POST | `/exam-goals` | 목표 목록 / `{title,exam_type,exam_date}`로 생성 |
| PATCH | `/exam-goals/{goalId}` | `{title?,exam_date?,status?}` |
| GET / POST | `/study-plans` | 계획 목록 / `{exam_goal_id,material_ids[]}`로 생성 |
| GET | `/study-plans/{planId}` | 계획과 task |
| POST | `/study-plans/{planId}/reschedule` | 남은 학습의 새 revision 생성 |
| PATCH | `/plan-tasks/{taskId}` | `{status:pending\|completed\|skipped}` |
| GET | `/today` | 선택 query `exam_goal_id`; 오늘의 계획·task·진행률 |

목표 종류는 `school_exam | toeic | toefl | entrance_exam | other`, 상태는 `active | paused | completed`입니다. 계획은 활성 목표, 준비된 비보관 자료와 현재 확정 개념 버전을 사용합니다. 계획 생성은 결정적 로직이며 추가 AI 호출을 하지 않습니다. 같은 목표에는 활성 revision이 하나만 존재합니다.

task는 `solve | teach_back | review`이며 개념 버전을 고정합니다. `concepts`, `concept_ids`, `concept_version_ids`, `title`, `display_order`, `completed_count`는 실제 관계/결과에서 계산한 응답 필드입니다. `solve`는 task에 연결된 서로 다른 채점 완료 문제 수, `teach_back`은 완료된 연결 세션을 기준으로 진행합니다. 문제 생성이나 자습 설명 생성만으로 완료되지 않습니다. `review`는 수동 완료가 가능합니다.

목표 날짜를 수정해도 기존 계획은 자동으로 다시 생성되지 않습니다. 명시적 reschedule을 사용합니다. 진행 중인 작업·오래된 revision과 충돌하면 요청이 거부될 수 있습니다. `days_remaining`은 선택된 목표 기준 `max(0, exam_date - 오늘)`이며 목표 scope가 없을 때만 null입니다.

## 문제 풀이·채점

| 메서드 | 경로 | 입력·결과 |
| --- | --- | --- |
| GET | `/questions/{questionId}` | B가 제공하는 공개 문제 DTO; 답안 키 제외 |
| POST | `/questions/{questionId}/attempts` | `{answer,question_set_id,model_id}` → `202` Job |
| GET | `/questions/{questionId}/attempts` | 소유한 시도 이력 |
| GET | `/attempts/{attemptId}` | 시도 상태와 완료된 채점 결과 |

객관식 answer는 `{type:"multiple_choice",selected_option_ids:["id"]}`로 정확히 한 선택지를, 단답형은 `{type:"short_answer",text:"…"}`를 전달합니다. 문제·세트·task·고정 버전 관계를 같은 DB에서 검증합니다.

시도 상태는 **`grading | graded | failed`**입니다. Job의 성공 상태 `succeeded`와 혼동하지 않습니다. `result`는 `graded`일 때만 공개됩니다. 동일 문제의 과거 Job으로 새 시도를 덮어쓰거나 task 진행을 중복 반영하지 않도록 현재 시도와 Job을 확인합니다.

## Teach-back

| 메서드 | 경로 | 입력·결과 |
| --- | --- | --- |
| POST | `/teachback-sessions` | `{concept_version_id,task_id?,attempt_id?,previous_session_id?,explanation_language,student_persona}` |
| GET / PATCH | `/teachback-sessions/{sessionId}` | 세션 조회 / `{status:"abandoned"}` |
| GET / POST | `/teachback-sessions/{sessionId}/messages` | 순서대로 조회 / `{content,input_mode,model_id}` → `202` |
| POST | `/teachback-sessions/{sessionId}/final-answer` | 마지막 답변 `{content,input_mode}` 저장; AI 호출 없음 |
| POST | `/teachback-sessions/{sessionId}/complete` | `{model_id}` → 리포트 생성 Job `202` |
| GET | `/teachback-sessions/{sessionId}/report` | 완료 리포트; 준비 전 `409` |
| POST | `/teachback-sessions/{sessionId}/feedback` | `{report_item_id?,reason,comment?}` → 신고 접수 |

언어는 `ko | en`, persona는 `curious_beginner | confused_friend | exam_candidate`, 입력은 `text | voice_transcript`입니다. 음성 파일 업로드나 음성 인식 자체를 제공하는 것은 아닙니다.

세션 상태는 `active | report_pending | completed | abandoned`입니다. assistant 후속 질문은 최대 3회이며 마지막 사용자 답변은 별도 `final-answer`로 기록합니다. 대화와 리포트 Job은 동일 세션을 잠그고 소유자·현재 Job·상태를 검사합니다.

리포트는 고정 개념 버전의 전체 checklist 항목을 평가하고 항목·근거를 정규화된 행에 저장합니다. coverage는 `weighted-v1` 계산 결과이며 provider가 보낸 점수를 그대로 신뢰하지 않습니다. 평가 가능한 항목이 없으면 coverage는 null입니다. 피드백 접수는 기존 평가나 coverage를 자동 수정하지 않습니다.

보고서 작업이 terminal인 `report_pending` 세션은 저장된 보고서가 없을 때 PATCH로 포기할 수 있습니다. `queued/running`은 `409`이고 완료 보고서·상태는 변경하지 않습니다. 이미 포기한 세션에 같은 PATCH를 보내면 `200`으로 재응답합니다. 계획/목표가 비활성이어도 정리가 가능하며 대화·Job 이력·latest pointer는 보존합니다. 포기 후 기존 Job은 재시작하거나 늦은 결과로 세션을 완료시킬 수 없습니다.

## 기록과 개선점

| 메서드 | 경로 | 내용 |
| --- | --- | --- |
| GET | `/records/sessions` | 개인 세션 이력 |
| GET | `/records/weaknesses` | 개념별 오답·Teach-back·coverage 변화 |
| GET / PATCH | `/records/actions`, `/records/actions/{actionId}` | 개선 행동 조회 / 상태·후속 세션 연결 |
| GET | `/records/public-attempts` | 저장된 개인 공개 문제 풀이 snapshot 조회 |

coverage 변화는 같은 개념 버전과 같은 계산식의 비교 가능한 리포트로 판단합니다. 공개 풀이 이력 조회가 있다는 사실이 공개 게시·공개 풀이 생성 API까지 제공한다는 뜻은 아닙니다.

## 비동기 연결

`grade`, `message`, `report` handler는 앱 조립 시 실제 공유 worker에 등록됩니다. 입력 검증, credential vault, provider adapter, 출력 검증, fencing, 결과 저장을 같은 Job 흐름으로 처리합니다. `services/job_results.py`의 과거 직접 완료 함수는 통합 완료 경로로 사용하지 않습니다.

`202`를 받으면 `GET /jobs/{jobId}`를 조회합니다. `queued | running | succeeded | failed | outcome_unknown`을 처리하고 성공 시 결과 리소스를 다시 읽습니다. 실패/불확실 결과의 재처리는 공통 `/retry`, `/restart` 정책을 사용합니다. API 종류별 상태와 Job 상태를 같은 enum으로 취급하지 않습니다.

현재 검증은 [검증 기록](VALIDATION.md)과 해당 commit의 CI 결과로 확인합니다. SQLite 성공만으로 MySQL migration·잠금 동작이나 운영 DB 호환성을 주장하지 않습니다.
