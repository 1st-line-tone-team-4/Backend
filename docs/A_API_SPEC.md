# A파트 API 명세 및 구현 체크리스트

이 문서는 현재 `language_learning_api` 코드에서 제공하는 A파트 API를 기능별로 모았습니다. `openapi-a.json`은 Swagger UI에서 가져올 수 있는 OpenAPI 파일입니다.

> 체크박스 `[x]`는 해당 HTTP 경로와 핸들러가 코드에 있다는 뜻입니다. MySQL·B파트 worker를 포함한 end-to-end 완료를 뜻하지 않습니다.

## 공통 규칙

- Base path: `/v1`
- 인증: 아래 인증 제외 API를 빼고 `Authorization: Bearer <JWT>` 필요. JWT 발급은 기존 인증 제공자가 담당합니다.
- 개인 리소스는 로그인 사용자 소유인지 확인합니다. 다른 사용자의 ID는 `404 NOT_FOUND`로 응답합니다.
- UUID는 리소스 ID로 사용하고, 날짜는 Asia/Seoul 기준, timestamp는 UTC로 처리합니다.
- POST 요청은 명세된 경우 `Idempotency-Key` 헤더가 필요합니다. 같은 키와 같은 요청은 기존 응답을 돌려주고, 다른 요청 본문이면 `409 IDEMPOTENCY_CONFLICT`입니다.
- 오류 응답 형식: `{"error":{"code":"...","message":"...","request_id":"...","details":[]}}`
- 공통 상태 코드: `400`, `401`, `403`, `404`, `409`, `422`, `429`, `503`. 각 API의 상태 충돌은 아래 표를 참고하세요.

## 사용자와 오늘 학습

| 완료 | 메서드 / 경로 | 요청 | 성공 응답 |
|---|---|---|---|
| [x] | `GET /v1/me` | Bearer JWT | `200 {data:{id,email,display_name,timezone}}` |
| [x] | `GET /v1/today` | 선택 query `exam_goal_id` | `200 {data:{date,progress,tasks,next_action,exam_goal_id,study_plan_id,days_remaining}}` |

오늘 task는 `due_date <= 오늘`인 활성 계획 task를 `display_order` 순으로 반환합니다. `progress`는 완료 수 / (건너뜀을 제외한 task 수)이며, 분모가 0이면 0입니다. `days_remaining`은 시험일까지 남은 날짜로, 시험 당일과 지난 시험은 0입니다. 특정 비활성 목표를 지정하면 목표 ID와 D-day는 반환하고 계획·task는 비웁니다.

## 시험 목표

| 완료 | 메서드 / 경로 | 요청 | 성공 응답 / 주요 오류 |
|---|---|---|---|
| [x] | `GET /v1/exam-goals` | query `cursor?`, `limit?` (기본 20, 최대 100) | `200 {data:ExamGoal[],next_cursor}` |
| [x] | `POST /v1/exam-goals` | `Idempotency-Key`; `{title,exam_type,exam_date}` | `201 {data:ExamGoal}`; 지난 날짜 `422 EXAM_PASSED` |
| [x] | `PATCH /v1/exam-goals/{goalId}` | `{title?,exam_date?,status?}` | `200 {data:ExamGoal}`; 잘못된 상태/지난 날짜 `422` |

`exam_type`: `school_exam | toeic | toefl | entrance_exam | other`. `status`: `active | paused | completed`. `ExamGoal`에는 `id,title,exam_type,exam_date,status,course_preview`가 포함됩니다. `course_preview`에는 `days_remaining,eligible,reason,stages`가 있습니다.

## 학습 계획과 할 일

| 완료 | 메서드 / 경로 | 요청 | 성공 응답 / 주요 오류 |
|---|---|---|---|
| [x] | `POST /v1/study-plans` | `Idempotency-Key`; `{exam_goal_id,material_ids[]}` | `201 {data:StudyPlan}`; 이미 활성 계획 `409 ACTIVE_PLAN_EXISTS`; 자료/개념 미준비 `422` |
| [x] | `GET /v1/study-plans` | query `cursor?`, `limit?`, `exam_goal_id?`, `status?` | `200 {data:StudyPlanSummary[],next_cursor}` |
| [x] | `GET /v1/study-plans/{planId}` | 없음 | `200 {data:StudyPlan}` |
| [x] | `POST /v1/study-plans/{planId}/reschedule` | `Idempotency-Key`, body 없음 | `201 {data:StudyPlan}`; 남은 task 없음 `422 NO_REMAINING_TASKS`; 진행 중 작업 `409 JOB_IN_PROGRESS` |
| [x] | `PATCH /v1/plan-tasks/{taskId}` | `{status:pending\|completed\|skipped}` | `200 {data:PlanTask}`; 문제풀이/Teach-back 완료 수동 변경 `422` |

계획은 활성 시험 목표, 준비 완료 자료, 확정된 개념 버전을 바탕으로 생성됩니다. 하루 공부 시간은 입력받지 않습니다. task에는 `id,due_date,task_type,concept_ids,concept_version_ids,target_count,completed_count,status,completed_at,title,display_order,concepts`가 포함됩니다. task 종류는 `solve | teach_back | review`입니다. `solve`와 `teach_back` 완료량은 각 풀이·세션 결과에서 갱신됩니다. `review`만 수동 완료할 수 있습니다.

## 문제 풀이와 채점

문제 세트·문제 생성은 B파트입니다. 아래 API는 B파트가 생성한 본인 문제를 읽고 풀이를 제출합니다.

| 완료 | 메서드 / 경로 | 요청 | 성공 응답 / 주요 오류 |
|---|---|---|---|
| [x] | `GET /v1/questions/{questionId}` | 없음 | `200 {data:Question}`; 정답 키는 반환하지 않음 |
| [x] | `POST /v1/questions/{questionId}/attempts` | `Idempotency-Key`, `X-AI-Key`; `{answer,question_set_id,model_id}` | `202 {data:{job:Job}}`; 채점 중복 `409 JOB_IN_PROGRESS` |
| [x] | `GET /v1/questions/{questionId}/attempts` | query `cursor?`, `limit?` | `200 {data:QuestionAttempt[],next_cursor}` |
| [x] | `GET /v1/attempts/{attemptId}` | 없음 | `200 {data:QuestionAttempt}` |

객관식 답은 `{type:"multiple_choice",selected_option_ids:["..."]}`이고 선택지는 하나입니다. 단답형은 `{type:"short_answer",text:"..."}`입니다. `QuestionAttempt`에는 답안, `grading | succeeded | failed` 상태, 채점 결과(완료 후), `job_id`, `answered_at`이 포함됩니다.

## Teach-back 세션

| 완료 | 메서드 / 경로 | 요청 | 성공 응답 / 주요 오류 |
|---|---|---|---|
| [x] | `POST /v1/teachback-sessions` | `Idempotency-Key`; `{concept_version_id,task_id?,attempt_id?,previous_session_id?,explanation_language,student_persona}` | `201 {data:TeachbackSession}`; 해당 task 활성 세션 중복 `409` |
| [x] | `GET /v1/teachback-sessions/{sessionId}` | 없음 | `200 {data:TeachbackSession}` |
| [x] | `PATCH /v1/teachback-sessions/{sessionId}` | `{status:"abandoned"}` | `200 {data:TeachbackSession}`; 진행 중 Job `409 JOB_IN_PROGRESS` |
| [x] | `GET /v1/teachback-sessions/{sessionId}/messages` | query `cursor?`, `limit?` | `200 {data:SessionMessage[],next_cursor}`; 대화 순서대로 반환 |
| [x] | `POST /v1/teachback-sessions/{sessionId}/messages` | `Idempotency-Key`, `X-AI-Key`; `{content,input_mode,model_id}` | `202 {data:{job:Job}}`; 활성 Job `409`, 질문 3회 초과 `422 QUESTION_LIMIT_REACHED` |
| [x] | `POST /v1/teachback-sessions/{sessionId}/complete` | `Idempotency-Key`, `X-AI-Key`; `{model_id}` | `202 {data:{job:Job}}`; 사용자 설명 없음 `422 MESSAGE_REQUIRED` |
| [x] | `GET /v1/teachback-sessions/{sessionId}/report` | 없음 | `200 {data:SessionReport}`; 아직 생성 전 `409 REPORT_NOT_READY` |
| [x] | `POST /v1/teachback-sessions/{sessionId}/feedback` | `Idempotency-Key`; `{report_item_id?,reason,comment?}` | `201 {data:FeedbackReport}`; 신고로 원평가를 자동 변경하지 않음 |
| [x] | `POST /v1/teachback-sessions/{sessionId}/final-answer` | `Idempotency-Key`; `{content,input_mode}` | `201 {data:SessionMessage}`; assistant 질문 3회 후 마지막 답변 제출 |

`explanation_language`: `ko | en`. `student_persona`: `curious_beginner | confused_friend | exam_candidate`. `input_mode`: `text | voice_transcript`. `reason`: `incorrect | unsupported | unclear | other`. session 상태는 `active | report_pending | completed | abandoned`입니다.

## 학습 기록과 취약점

| 완료 | 메서드 / 경로 | 요청 | 성공 응답 |
|---|---|---|---|
| [x] | `GET /v1/records/sessions` | query `cursor?`, `limit?` | `200 {data:SessionSummary[],next_cursor}` |
| [x] | `GET /v1/records/weaknesses` | query `cursor?`, `limit?` | `200 {data:Weakness[],next_cursor}` |
| [x] | `GET /v1/records/actions` | query `cursor?`, `limit?` | `200 {data:ImprovementAction[],next_cursor}` |
| [x] | `PATCH /v1/records/actions/{actionId}` | `{status,followup_session_id?}` | `200 {data:ImprovementAction}` |
| [x] | `GET /v1/records/public-attempts` | query `cursor?`, `limit?` | `200 {data:PublicAttempt[],next_cursor}` |

취약점에는 `concept_id,concept_name,concept_version_id,latest_coverage,incorrect_attempts,teachback_count,trend`가 포함됩니다. trend는 같은 개념 버전의 최근 리포트 2건을 비교해 `improving | stable | declining | unknown`으로 반환합니다.

## B파트/공통 모듈이 연결되어야 하는 기능

- B파트는 자료, 단원, 개념 버전, 문제 세트와 문제 데이터를 공급합니다. 이 데이터가 없으면 계획 생성과 문제 API는 유효한 리소스가 없어 진행되지 않습니다.
- 문제 채점과 Teach-back 메시지·리포트 생성은 `202`로 Job을 만들며, 결과는 `services/job_results.py`의 worker 콜백(`complete_grade`, `complete_message`, `complete_report`)으로 반영합니다.
- 공통 Job 조회 `GET /v1/jobs/{jobId}`, worker 실행·실패/재시도, BYOK 키의 암호화 단기 저장은 이 A API 파일에 포함하지 않습니다. 실제 AI 작업 완료에는 B/공통 worker 연결이 필요합니다.
- `GET /v1/health`는 인증 없는 점검용 경로입니다.
