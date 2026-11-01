# 전체 API 명세서

- API 이름: 1호선톤 학습 서비스 API — 개발 착수 기준 제안
- 계약 버전: 1.3.0-backend-proposal
- Base URL: `/v1`
- 전체 규모: **50개 경로 / 64개 작업**
- 이 문서는 전체 계약을 정리한 문서이며 구현 완료 표시가 아닙니다. 현재 원본의 버전 이름은 `backend-proposal`이므로 제안 상태와 구현 상태를 구분해서 검토하세요.
- 기계 판독용 원본: [`openapi-full.json`](openapi-full.json) (Swagger UI에서 Import 가능)

## 공통 계약

- 기본 인증은 Bearer JWT이며 로그인과 토큰 발급은 기존 인증 제공자에서 담당합니다.
- timestamp는 UTC RFC3339, 서비스 날짜와 D-day 계산은 Asia/Seoul입니다.
- 모든 POST는 `Idempotency-Key`를 요구합니다. AI 작업 요청은 `X-AI-Key`도 요구합니다.
- 202 응답은 작업 접수이며 작업 완료 응답이 아닙니다. 상태는 `GET /jobs/{jobId}`로 확인합니다.
- 개인 리소스의 소유권 불일치는 404, 관계/입력 계약 위반은 422, 상태 충돌은 409로 처리합니다.
- 기본 목록은 cursor 기반이며 `limit` 기본값은 20, 최대 100입니다.

## 담당 범위

- **A 파트**: 사용자/목표/계획/오늘 학습, 문제 풀이·채점 요청, Teach-back, 학습 기록과 취약점.
- **B 파트**: 자료·개념·문제세트·자습 설명, 문제 공유·성적 인증.
- **공통/B 파트**: AI 설정, BYOK, 비동기 Job/worker와 복구, 구독 capability 조회.

## 엔드포인트 전체 목록

### A · 사용자, 시험 목표와 오늘 학습

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `GET` | `/me` | `getMe` | 없음 | 200 `data:UserProfile` |
| `GET` | `/exam-goals` | `listExamGoals` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 |
| `POST` | `/exam-goals` | `createExamGoal` | `header Idempotency-Key` (필수, string); Body: `ExamGoalInput` | 201 `data:ExamGoal` |
| `PATCH` | `/exam-goals/{goalId}` | `updateExamGoal` | Body: `ExamGoalPatch` | 200 `data:ExamGoal` |
| `GET` | `/today` | `getToday` | `query exam_goal_id` (선택, string) | 200 `data:TodaySummary` |

#### `POST /exam-goals` — `createExamGoal`

exam_date는 Asia/Seoul 오늘 이상. 180일 초과도 목표 저장은 허용하되 course_preview.eligible=false. 상세 계획 생성은0..180일만. daily_minutes/proficiency_level은 입력·DB 모두 제거. 기존 계획은 목표 변경으로 덮어쓰지 않으며 explicit reschedule 필요.

#### `PATCH /exam-goals/{goalId}` — `updateExamGoal`

전체 ExamGoal을 반환한다. 날짜 변경 후 기존 계획은 immutable이며 명시적 reschedule. 과거로 날짜 변경422.

#### `GET /today` — `getToday`

Asia/Seoul 오늘 날짜를 date로 반환한다. exam_goal_id 지정 시 본인 목표만(타인/없음404); 해당 목표가 inactive이거나 active plan이 없으면 exam_goal_id는 유지하고 study_plan_id=null, tasks=[],progress=0,next_action=null이다. 미지정 시 본인 active goal+active plan 조합 중 plan.generated_at DESC,id DESC 첫 계획 하나를 선택하며 목표를 혼합하지 않는다. 선택 조합이 없으면 두 scope ID와 days_remaining은 null이다. days_remaining은 선택한 목표가 있으면 max(0,exam_date-date)의 달력 일수로 필수 반환한다. 당일 및 지난 시험은 0, 계획 부재와 목표 부재는 구분한다. 해당 plan의 due_date<=date tasks 전체(최대6000)를 display_order ASC로 반환하며 title,concepts의 표시명과 정확한 버전 ID를 포함한다. progress=completed/(전체-skipped),분모0은0. next_action은 선택 plan의 pinned concept들 중 가장 최근 pending action.text,없으면null. 집계는 페이지가 아닌 선택 scope 전체이다.

### B · 자료, 개념과 근거

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `POST` | `/materials` | `createMaterialUpload` | `header Idempotency-Key` (필수, string); Body: `filename*`:string, `mime_type*`:string[application/pdf|text/plain|image/png|image/jpeg], `size_bytes*`:integer | 201 |
| `GET` | `/materials` | `listMaterials` | `query cursor` (선택, string); `query limit` (선택, integer); `query include_archived` (선택, boolean) | 200 |
| `POST` | `/materials/{materialId}/complete` | `completeMaterialUpload` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `model_id*`:string | 202 `data:Accepted` |
| `GET` | `/materials/{materialId}` | `getMaterial` | 없음 | 200 `data:Material` |
| `DELETE` | `/materials/{materialId}` | `deleteMaterial` | 없음 | 204 |
| `PATCH` | `/materials/{materialId}` | `archiveMaterial` | Body: `archived*`:boolean | 200 `data:Material` |
| `GET` | `/materials/{materialId}/concepts` | `listConcepts` | 없음 | 200 |
| `PATCH` | `/materials/{materialId}/concepts` | `confirmConcepts` | Body: `concepts*`:array | 200 |
| `GET` | `/materials/{materialId}/concepts/{conceptId}/versions` | `listConceptVersions` | 없음 | 200 |
| `GET` | `/materials/{materialId}/units` | `listMaterialUnits` | 없음 | 200 |
| `GET` | `/materials/{materialId}/source` | `getMaterialSource` | `query page` (필수, integer) | 200 |
| `GET` | `/materials/{materialId}/chunks/{chunkId}` | `getMaterialChunk` | 없음 | 200 `data:SourceEvidence` |

#### `POST /materials` — `createMaterialUpload`

서명 PUT URL 유효10분. MIME/크기/파일 magic 검사, PDF/TXT/PNG/JPEG만, 최대20MiB·PDF200페이지. 본인 원본은 private storage. storage_key는 외부 입력 금지.

#### `POST /materials/{materialId}/complete` — `completeMaterialUpload`

실제 업로드 크기/MIME 확인 후 최초 분석. unit,concept,draft version,summary,checklist,evidence를 한 번에 저장. TXT/이미지는 page_number=1. 최초 complete만 허용, 실패는 jobs retry. 자료당 활성 job 1개.

#### `DELETE /materials/{materialId}` — `deleteMaterial`

plan/question/session/publication/활성job 참조가 있으면409 MATERIAL_IN_USE: archive 사용. 참조 없는 자료만 DB 소유 하위 분석 레코드 삭제 및 outbox에 원본 제거 기록(24시간 내). FK RESTRICT; implicit cascade 금지. 이력과 공개 문제를 몰래 지우지 않음. learning_explanations 또는 그 근거 참조가 있어도409 MATERIAL_IN_USE이며 archive로 이력을 유지한다.

#### `PATCH /materials/{materialId}` — `archiveMaterial`

숨김/복구만. archived 자료는 목록 기본 제외, 기존 이력·근거는 열람 가능. 새 생성 요청에는 사용할 수 없다.

#### `GET /materials/{materialId}/concepts` — `listConcepts`

ready 이전에는data=[];ready일때concept와초기draft version이동시에존재. current_version은최대version_no의draft 또는confirmed 객체(전체null아님). 확정전summary null 가능. 확정은summary/기준/근거필수. current version이draft면계획/문제생성422 CONCEPT_NOT_CONFIRMED.

#### `PATCH /materials/{materialId}/concepts` — `confirmConcepts`

일괄 원자적 확정. 새 개념은 concept_id/base_version_id=null. 수정은 current version과 base_version_id 일치해야 하며 다르면409 VERSION_CONFLICT. 새 immutable confirmed version 생성, 기존 버전·기준·계획/문제/평가를 수정하지 않음. unit/chunk는 같은 material; 요약·기준·근거 비어 있으면422. is_current는 가장 큰 version_no, 과거 draft를 confirmed로 덮어쓰지 않음.

#### `GET /materials/{materialId}/units` — `listMaterialUnits`

listMaterialUnits

#### `GET /materials/{materialId}/source` — `getMaterialSource`

소유권 검사 후 원본 signed GET URL5분. PDF는 프런트 page fragment로 페이지 이동, TXT/이미지1페이지. 범위초과422. 원본URL은 공유 DTO/로그에 포함 금지.

#### `GET /materials/{materialId}/chunks/{chunkId}` — `getMaterialChunk`

chunk와 material 관계 및 소유권 확인. 전체 chunk text는 excerpt 필드로 반환, private 조회만.

### A · 학습 계획과 할 일

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `POST` | `/study-plans` | `createStudyPlan` | `header Idempotency-Key` (필수, string); Body: `exam_goal_id*`:string, `material_ids*`:array | 201 `data:StudyPlan` |
| `GET` | `/study-plans` | `listStudyPlans` | `query cursor` (선택, string); `query limit` (선택, integer); `query exam_goal_id` (선택, string); `query status` (선택, string (active, superseded)) | 200 |
| `GET` | `/study-plans/{planId}` | `getStudyPlan` | 없음 | 200 `data:StudyPlan` |
| `POST` | `/study-plans/{planId}/reschedule` | `rescheduleStudyPlan` | `header Idempotency-Key` (필수, string) | 201 `data:StudyPlan` |
| `PATCH` | `/plan-tasks/{taskId}` | `updatePlanTaskStatus` | Body: `status*`:string[pending|completed|skipped] | 200 `data:PlanTask` |

#### `POST /study-plans` — `createStudyPlan`

동기적 결정론적 계획(추가AI/비용 없음). active goal, 오늘~180일, ready·not archived 자료와 confirmed current versions 필요. 각 unit의 개념별 solve5/teach_back1/review1 task를 순서대로 생성. material 입력순→unit.ordinal→concept.created_at,id 순, 각 개념 solve→teach_back→review. N개 task의 i(0-based) due=today+floor(i*D/N), D=max(1,시험일까지 일수), 시험 전일 이내; 당일시험은 오늘. 목표당 active plan1개, 중복409 ACTIVE_PLAN_EXISTS. 시간 추정/일일시간 저장 없음. 상세tasks최대6000(자료20×자료별concept100×3). 초과시422 RESOURCE_LIMIT_REACHED,조용히자르지않음. PlanTask의 title/display_order/concepts 표시 계약을 적용하며 tasks는 display_order ASC이다.

#### `GET /study-plans` — `listStudyPlans`

 목록은StudyPlanSummary(material_ids/task_count, tasks제외),상세GET에만tasks전체. material_ids는study_plan_materials.ordinal순.

#### `GET /study-plans/{planId}` — `getStudyPlan`

 PlanTask의 title/display_order/concepts 표시 계약을 적용하며 tasks는 display_order ASC이다.

#### `POST /study-plans/{planId}/reschedule` — `rescheduleStudyPlan`

active plan만 허용. goal 잠금, 새 revision/ID. 완료task는 새 revision에 복제하지 않고 이전 이력 유지; pending/in_progress/skipped 작업을 남은 날짜에 균등 배치, task version 핀 유지. 기존 revision superseded. 이전 task 결과는 남지만 새 task 완료량에는 자동 합산하지 않음. 남은작업0이면422 NO_REMAINING_TASKS. 구버전 task에 신규학습 시작409. 새task.target_count는max(1,이전target_count-이전completed_count). 남은작업은기존ordinal순. 재배치에활성session/grade/questionjob 있으면409 JOB_IN_PROGRESS,완료/포기후재요청. 잔여tasks<=6000 유지,초과면422 RESOURCE_LIMIT_REACHED,조용히절단안함. PlanTask의 title/display_order/concepts 표시 계약을 적용하며 tasks는 display_order ASC이다.

#### `PATCH /plan-tasks/{taskId}` — `updatePlanTaskStatus`

review만 completed 수동 가능, solve/teach_back completed는 실적 집계만(수동요청422). skipped는 미완료task만. pending은 skipped/review completed 취소만. solve는 서로다른 question의 succeeded grade 개수, teach_back은 completed session 개수; 한 task와 같은 버전 범위만. target이상 자동completed+completed_at, 미만1개이상in_progress. skipped는 today progress 분모에서 제외; 분모0 progress0.

### A · 문제 풀이와 채점

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `GET` | `/questions/{questionId}` | `getQuestion` | 없음 | 200 `data:Question` |
| `POST` | `/questions/{questionId}/attempts` | `submitAttempt` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `answer*`:QuestionAnswer, `question_set_id*`:string, `model_id*`:string | 202 `data:Accepted` |
| `GET` | `/questions/{questionId}/attempts` | `listQuestionAttempts` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 `QuestionAttemptPage` |
| `GET` | `/attempts/{attemptId}` | `getAttempt` | 없음 | 200 `data:QuestionAttempt` |

#### `POST /questions/{questionId}/attempts` — `submitAttempt`

본인 set의 question에만 제출. 선택형 옵션2..5/정답1개, selected_option_ids 정확히1개. short_answer는 빈칸 문자열 또는 짧은답, text1..12000. 타입/옵션/관계 검증422. 한 question당 활성grade1개. 정답도 grade job으로 동일 계약, answer_key는 제출전 노출 금지. 결과와 집계는 트랜잭션, 같은 question 재시도는 task count 중복 증가 안함.

#### `GET /attempts/{attemptId}` — `getAttempt`

getAttempt

### A · Teach-back 세션과 리포트

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `POST` | `/teachback-sessions` | `createTeachbackSession` | `header Idempotency-Key` (필수, string); Body: `concept_version_id*`:string, `task_id`:없음, `attempt_id`:없음, `previous_session_id`:없음, `explanation_language*`:string[ko|en], `student_persona*`:string[curious_beginner|confused_friend|exam_candidate] | 201 `data:TeachbackSession` |
| `GET` | `/teachback-sessions/{sessionId}` | `getTeachbackSession` | 없음 | 200 `data:TeachbackSession` |
| `PATCH` | `/teachback-sessions/{sessionId}` | `abandonSession` | Body: `status*`:string[abandoned] | 200 `data:TeachbackSession` |
| `GET` | `/teachback-sessions/{sessionId}/messages` | `listTeachbackMessages` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 `SessionMessagePage` |
| `POST` | `/teachback-sessions/{sessionId}/messages` | `sendTeachbackMessage` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `content*`:string, `input_mode*`:string[text|voice_transcript], `model_id*`:string | 202 `data:Accepted` |
| `POST` | `/teachback-sessions/{sessionId}/complete` | `completeTeachbackSession` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `model_id*`:string | 202 `data:Accepted` |
| `GET` | `/teachback-sessions/{sessionId}/report` | `getTeachbackReport` | 없음 | 200 `data:SessionReport` |
| `POST` | `/teachback-sessions/{sessionId}/feedback` | `reportIncorrectFeedback` | `header Idempotency-Key` (필수, string); Body: `report_item_id`:string/null, `reason*`:string[incorrect|unsupported|unclear|other], `comment`:string/null | 201 `data:FeedbackReport` |
| `POST` | `/teachback-sessions/{sessionId}/final-answer` | `submitFinalTeachbackAnswer` | `header Idempotency-Key` (필수, string); Body: `content*`:string, `input_mode*`:string[text|voice_transcript] | 201 `data:SessionMessage` |

#### `POST /teachback-sessions` — `createTeachbackSession`

confirmed version 소유권 검사. task는 teach_back+정확한 버전; attempt/previous는 본인 같은 concept(다른 버전 허용, 비교는 동버전만). task별 active session 중복409. 초기 active, 질문수0, 자동 메시지/AI호출 없음. attempt참조는graded일때만,failed/grading은422.

#### `PATCH /teachback-sessions/{sessionId}` — `abandonSession`

active이고 활성job 없을 때만 abandoned. 완료/보고서대기 세션 변경409. 버린 세션 메시지 이력 보존.

#### `POST /teachback-sessions/{sessionId}/messages` — `sendTeachbackMessage`

active에서 사용자 설명 완료 후 한 번 전송(녹음중 호출 없음). browser STT로 변환하여 사용자가 확인한 텍스트만 전송; 원음 미저장, STT 불가시 text fallback. user message와 job 먼저 원자저장. 세션당활성job1개, sequence unique. assistant 질문은 최대3회;3회 후422 QUESTION_LIMIT_REACHED, complete만 가능. 성공시에만 question_count증가; 실패재시도는 기존user message 사용. 결과URL=messages 목록. 질문3개를받았다면 /final-answer로최종답변을비용없이제출하고complete. terminal실패는 jobs restart로같은user message 재사용.

#### `POST /teachback-sessions/{sessionId}/complete` — `completeTeachbackSession`

active+사용자메시지1개이상+활성message job없음. report_pending 전이. report job 실패시 report_pending 유지, job의 retry 정책 사용; 임의 새 complete로 중복 보고서 생성 금지. 성공시 report+모든criteria+action 생성 및 session completed 원자저장. 보고서 terminal실패/unknown은 jobs restart로동일session 복구한다. 질문3개후마지막답변을건너뛰는complete도허용하되생략내용은평가기준에따라누락/확인불가.

#### `GET /teachback-sessions/{sessionId}/report` — `getTeachbackReport`

완료report만 반환. 아직보고서없음409 REPORT_NOT_READY + latest job는 session조회. 다른소유자404. 불변평가기준 전체 결과.

#### `POST /teachback-sessions/{sessionId}/feedback` — `reportIncorrectFeedback`

본인 report만. report_item_id는 해당 session.report 항목이어야 함. reason/comment 접수만, 원평가 자동수정 없음.

#### `POST /teachback-sessions/{sessionId}/final-answer` — `submitFinalTeachbackAnswer`

active,question_count=3,마지막role=assistant,활성job없음일때만 세번째질문답변을user message로 저장. 추가AI질문/과금 없음. 저장후마지막role=user라재제출409(같은멱등키는같은응답). complete로보고서생성. 3회미만은일반messages 사용.

### A · 학습 기록과 취약점

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `GET` | `/records/weaknesses` | `listWeaknesses` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 |
| `GET` | `/records/sessions` | `listSessionRecords` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 `SessionSummaryPage` |
| `GET` | `/records/actions` | `listImprovementActions` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 |
| `PATCH` | `/records/actions/{actionId}` | `updateImprovementAction` | Body: `status*`:string[pending|attempted|completed], `followup_session_id*`:없음 | 200 `data:ImprovementAction` |
| `GET` | `/records/public-attempts` | `listPublicAttempts` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 |

#### `GET /records/weaknesses` — `listWeaknesses`

본인 current confirmed version별. 최근 completed report2개 중 유효coverage가 동버전·동formula인 경우만 비교;delta>0.0001 improving,<−0.0001 declining,그외stable. 미만unknown. 오답수는 distinct question의 최신graded attempt가 false인 수. 버전경계 비교 금지.

#### `GET /records/actions` — `listImprovementActions`

listImprovementActions

#### `PATCH /records/actions/{actionId}` — `updateImprovementAction`

사용자 자가보고. followup은 본인 같은 concept의 보고서 이후 session만. completed는 자기보고 완료이며 능력 향상 인증 아님. 보고서의 원문 next_action은 불변.

#### `GET /records/public-attempts` — `listPublicAttempts`

본인 풀이 snapshot. 철회 후에도 본인 결과 열람 가능; publication 페이지는404.

### 공통/B · AI 모델과 BYOK 설정

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `GET` | `/ai/models` | `listAIModels` | 없음 | 200 |
| `POST` | `/ai/validate-key` | `validateAIKey` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `model_id*`:string | 200 |

#### `GET /ai/models` — `listAIModels`

배포 허용목록. 모델 ID는 provider-qualified. 서버에 실제 활성화된 모델만 available=true; 임의 URL·임의 모델명 거부422. 하드코딩된 상용 모델 지원을 주장하지 않는다.

#### `POST /ai/validate-key` — `validateAIKey`

유효성 검사만, 앱 DB에 키 저장하지 않음. 실제 생성은 각 요청에 다시 X-AI-Key 전달. provider 권한 오류422 PROVIDER_AUTH. 비용 발생 없는 provider 검증이 불가능하면503 KEY_VALIDATION_UNAVAILABLE, 생성 호출로 몰래 검증하지 않는다.

### 공통/B · 비동기 Job과 복구

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `GET` | `/jobs/{jobId}` | `getJob` | 없음 | 200 `data:Job` |
| `POST` | `/jobs/{jobId}/retry` | `retryJob` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `model_id*`:string | 202 `data:Accepted` |
| `POST` | `/jobs/{jobId}/restart` | `restartTerminalJob` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `model_id*`:string, `acknowledge_possible_duplicate_cost*`:boolean | 202 `data:Accepted` |

#### `GET /jobs/{jobId}` — `getJob`

본인job, polling 최소2초/429 Retry-After. succeeded.result_url은 본인 GET결과. failed.retryable=true+attempt_no<3은retry; 그외failed/outcome_unknown은restart에새키+비용가능성동의. outcome_unknown자동재시도금지. 실패와 미확정은완료로표시하지않음. learning_explanation 성공의 result_url은 /v1/learning-explanations/{resource_id}; owner 보호 GET이며 사설 자료 외부 URL을 반환하지 않는다.

#### `POST /jobs/{jobId}/retry` — `retryJob`

failed+retryable=true, attempt_no<3만 허용. 같은 job ID attempt_no+1 원자 증가. 같은 모델만 허용. 키 재입력 필요. 과금 발생 전 확정된 credential/auth/rate_limit/unavailable 실패만 retryable. output/timeout/internal은 false. 실행 중 또는 성공한 job 재실행409. 동일하게현재resource.latest job검사와행잠금,후속전이금지. material/set/attempt실패상태,message sessionactive,report sessionreport_pending만. stale409. learning_explanation은 현재 latest_job_id이고 explanation.status=failed일 때만 같은 job의 attempt를 증가시키며 generating으로 복구한다. 저장 입력/버전/언어/모델은 불변, ready 및 outcome_unknown은 retry 금지.

#### `POST /jobs/{jobId}/restart` — `restartTerminalJob`

failed(재시도불가 또는3회소진) 또는 outcome_unknown 복구. 같은 model·resource·저장입력을 사용하여 새 job(previous_job_id=old.id,attempt_no=1)을 생성하고 resource.job_id를 교체; 새키와 중복provider과금가능성 명시동의true 필수. 이전job별 successor1개 UNIQUE. 전job은 terminal 불변, 늦은응답의 commit fencing 차단. analysis material→queued, set→generating, attempt→grading, report session→report_pending; message는 기존user message 재사용하며 새user message 생성안함. queued/running/succeeded/retryable failed(3회미만)는409. POST멱등원자트랜잭션. 사용자는 강제재시작 대신 기존실패이력 남기고 다른학습으로 이동가능. 필수사전조건: resource행을잠그고 old.id=현재latest_job_id(또는set/attempt.job_id),successor없음,다른activejob없음 검사. material=failed,set=failed,attempt=failed,message는session=active,report는session=report_pending만. 새메시지/완료/포기/성공 등후속전이가있으면409 STALE_JOB;과거실패job 부활금지. unknown은관련material/set/attempt를failed로표시하되job은outcome_unknown유지. restart원자transaction에서latest pointer교체. learning_explanation은 latest_job_id가 일치하는 failed 또는 outcome_unknown 리소스만 허용하며 새 job으로 포인터 교체와 generating 복구를 원자 수행한다. 같은 version/언어/모델/저장입력을 유지한다. ready 결과는 재시작/덮어쓰기 금지.

### B · 문제 세트 생성과 조회

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `POST` | `/question-sets` | `createQuestionSet` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `unit_id*`:string, `task_id`:없음, `concept_version_ids*`:array, `count*`:integer, `difficulty*`:string[easy|medium|hard], `model_id*`:string, `acknowledge_possible_duplicate_cost`:boolean | 202 `data:Accepted` |
| `GET` | `/question-sets` | `listQuestionSets` | `query cursor` (선택, string); `query limit` (선택, integer); `query task_id` (선택, string) | 200 |
| `GET` | `/question-sets/{setId}` | `getQuestionSet` | 없음 | 200 `data:QuestionSet` |

#### `POST /question-sets` — `createQuestionSet`

기존 /questions/generate 대체(1.2 breaking). unit·confirmed version·task는 같은 소유자/자료. task 있을 때 solve 및 정확한 버전 집합 일치, count는 남은목표 이하. set 생성 후 job.resource_id=set.id. 고정 ordinal1..count, 새 difficulty/count는 새 set. 같은 unit+version집합+task+count+difficulty의 활성job은409 JOB_IN_PROGRESS; outcome_unknown 전작업 존재 시 acknowledge_possible_duplicate_cost=true 없으면409 POSSIBLE_DUPLICATE_COST. 결과 atomic 저장.

#### `GET /question-sets` — `listQuestionSets`

listQuestionSets

#### `GET /question-sets/{setId}` — `getQuestionSet`

ready만 items 제공, generating/failed는 items=[]. next_ordinal=graded attempt가 없는 첫 ordinal; 모든 항목 graded면null. grading 중이면 기존 job을 따라가고 새 제출409. 오답도 진행완료이며 다시풀기는 새 attempt. latest_attempt는 본인 최신 answered_at,id.

### B · 문제 공유와 공개 풀이

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `POST` | `/publications` | `publishQuestion` | `header Idempotency-Key` (필수, string); Body: `question_id*`:string, `keywords*`:array, `consent_public*`:boolean | 201 `data:PublicQuestion` |
| `GET` | `/publications` | `listRecommendedQuestions` | `query cursor` (선택, string); `query limit` (선택, integer); `query keyword` (선택, string) | 200 |
| `GET` | `/publications/{publicationId}` | `getPublicQuestion` | 없음 | 200 `data:PublicQuestion` |
| `DELETE` | `/publications/{publicationId}` | `withdrawQuestion` | 없음 | 204 |
| `POST` | `/publications/{publicationId}/attempts` | `submitPublicAttempt` | `header Idempotency-Key` (필수, string); Body: `answer*`:QuestionAnswer | 201 `data:PublicAttempt` |

#### `POST /publications` — `publishQuestion`

본인 generated question만. consent_public=true 필수(false422), 공유 권리와 개인정보 확인 후 명시적 게시. 문제/옵션/정답/해설만 snapshot, private evidence/자료명/이름/답안/키는 제외. keyword trim+NFKC+lowercase, 추천은 keyword exact-match+created_at desc,id tie(개인화AI없음). 자동공유 없음. 게시 후 내용수정 불가, 철회 후 재게시도 같은원본 새 publication. 본인 채점해설 유출 대신 생성 당시 공개용 해설 사용. 정규화후keyword 중복은제거하지않고422 DUPLICATE_KEYWORD로명시;keywords JSON배열저장,최대5개.

#### `GET /publications` — `listRecommendedQuestions`

로그인 이용자 검색. keyword 없으면 최신순. active publication만, 개인자료 참조/정답 제외. 공유 자체 권리검증은 사용자의 명시적 동의로 최소화; 권리분쟁 신고는 서비스 운영절차. verified_score는 VerifiedScore 또는 null이며 현재 verified+공개동의 상태를 조회 시 검증한다. 해당 publication의 최신 decided_at DESC,id DESC 1건이고 철회 시 즉시 재선택한다. 게시자 신원/성적이라고 추정하지 않는다.

#### `GET /publications/{publicationId}` — `getPublicQuestion`

getPublicQuestion verified_score는 VerifiedScore 또는 null이며 현재 verified+공개동의 상태를 조회 시 검증한다. 해당 publication의 최신 decided_at DESC,id DESC 1건이고 철회 시 즉시 재선택한다. 게시자 신원/성적이라고 추정하지 않는다.

#### `DELETE /publications/{publicationId}` — `withdrawQuestion`

게시자만 철회. 즉시 검색/새풀이404, 이미 제출한 본인 public-attempt snapshot과 점수증빙 이력은 보존. 다른사람 개인풀이 접근불가. 원본자료 삭제는 active/withdrawn publication 참조로 제한.

#### `POST /publications/{publicationId}/attempts` — `submitPublicAttempt`

공유풀이 추가AI비용 없음. 객관식 선택ID1개 exact; short_answer는 Unicode NFKC+trim+소문자+연속공백1개로 정규화한 허용답안 중 일치만. 공개용 정답/해설 snapshot 저장. 자연어 의미채점 미지원임을 UI 표시. 원본소유자는 자신의 공유문제로 점수인증 연결 불가.

### B · 성적 인증과 검토

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `POST` | `/score-claims` | `createScoreClaim` | `header Idempotency-Key` (필수, string); Body: `public_attempt_id*`:string, `exam_type*`:string[toeic|toefl|entrance_exam|other], `score*`:number, `max_score*`:number, `exam_date*`:string, `mime_type*`:string[application/pdf|image/png|image/jpeg], `size_bytes*`:integer, `consent_public_score*`:boolean | 201 |
| `GET` | `/score-claims` | `listMyScoreClaims` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 |
| `POST` | `/score-claims/{claimId}/submit` | `submitScoreClaim` | `header Idempotency-Key` (필수, string) | 200 `data:ScoreClaim` |
| `DELETE` | `/score-claims/{claimId}` | `withdrawScoreClaim` | 없음 | 204 |
| `GET` | `/review/score-claims` | `listScoreReviewQueue` | `query cursor` (선택, string); `query limit` (선택, integer) | 200 |
| `GET` | `/review/score-claims/{claimId}/proof` | `getScoreProof` | 없음 | 200 |
| `PATCH` | `/review/score-claims/{claimId}` | `reviewScoreClaim` | Body: `status*`:string[verified|rejected], `reason*`:string | 200 `data:ScoreClaim` |

#### `POST /score-claims` — `createScoreClaim`

본인 public attempt와 이후(동일날포함) 실제 시험날짜<=오늘,0<=score<=max_score. 시험유형 toeic max990,toefl120,나머지 입력값. score는 학습효과 인과 증명 아님. 개인정보 가린 성적표 private PUT10분. 동일 attempt+exam_date당 active claim1개. 점수는0..99999999.99 소수둘째자리이하만허용(초과422,반올림하지않음). consent_public_score=true로검증후점수/시험종류/시험날짜가해당공유문제에공개됨에명시동의필수. 증빙원본/이름은공개안함.동의하지않으면이인증기능을사용하지않으며개인학습계속가능. DELETE로공개철회.

#### `GET /score-claims` — `listMyScoreClaims`

listMyScoreClaims

#### `POST /score-claims/{claimId}/submit` — `submitScoreClaim`

업로드 magic/size 검사 후 submitted. 수동검증 전 인증 배지 없음. 원본 삭제기한 verified/rejected/withdrawn 결정후30일; 미제출파일24시간, 미결submitted90일 만료rejected. score_claim는 proof_deleted_at으로 만료표현.

#### `DELETE /score-claims/{claimId}` — `withdrawScoreClaim`

본인 claim withdrawn, 공개 배지 즉시 제외. 증빙30일내제거. 재제출은 새 claim.

#### `GET /review/score-claims` — `listScoreReviewQueue`

서버 신뢰 JWT reviewer scope만403. 운영자 지정/검토기한 약속 아님. submitted만.

#### `GET /review/score-claims/{claimId}/proof` — `getScoreProof`

reviewer scope+submitted만 private signedGET5분, 접속감사. 일반사용자 및 publication에 절대공개 안함. 감사는기존구조화보안로그(actor,action,claimId,time)에기록하고증빙본문/URL/키는제외;전용감사DB테이블추가없음. 감사로그최소90일보존(백엔드권고),로그접근은운영권한만.

#### `PATCH /review/score-claims/{claimId}` — `reviewScoreClaim`

reviewer scope,본인claim 검토금지,submitted만 CAS. 증빙의 시험종류/점수/날짜 일치 및 식별불가/조작의심 없음 확인한 경우만 verified; 자동인증 금지. 법적진위보증 아님. 공개 배지는 해당 publication에서 최신verified claim1개; 만점 대비비율 비교하지 않음. proof만료 후 새검토불가. 공개 필드는 VerifiedScore만 사용하며 최신 기준은 decided_at DESC,id DESC이다. verified 전환에는 decided_at과 기존 공개동의 시각이 필수이고 철회 즉시 모든 목록/상세 캐시에서도 제외한다.

### 공통 · 구독 정책 정보

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `GET` | `/billing/capabilities` | `getBillingCapabilities` | 없음 | 200 |

#### `GET /billing/capabilities` — `getBillingCapabilities`

월구독 기획은 유지하며 가격은 추후 결정. checkout/환불/구독 생성 기능은 이번 범위에 추가하지 않는다. BYOK provider 실제 비용은 해당 제공자 정책에 따른다.

### B · 자습용 개념 설명

| 메서드 | 경로 | 작업 ID | 요청 | 성공 응답 |
|---|---|---|---|---|
| `POST` | `/learning-explanations` | `createLearningExplanation` | `header Idempotency-Key` (필수, string); `header X-AI-Key` (필수, string); Body: `LearningExplanationInput` | 202 `data:Accepted` |
| `GET` | `/learning-explanations` | `listLearningExplanations` | `query concept_version_id` (필수, string); `query cursor` (선택, string); `query limit` (선택, integer) | 200 `LearningExplanationPage` |
| `GET` | `/learning-explanations/{explanationId}` | `getLearningExplanation` | 없음 | 200 `data:LearningExplanation` |

#### `POST /learning-explanations` — `createLearningExplanation`

강의자료에 근거한 독립 자습 설명을 비동기로 생성한다. 소유 confirmed concept_version_id만 받으며 과거 confirmed 버전도 허용한다(초기 draft는422 CONCEPT_NOT_CONFIRMED). version→concept→material 소유권 및 ready/not archived 상태를 검사한다(타인/없음404, 본인 상태 부적합422). 버전의 요약/체크리스트/근거 청크를 고정 입력으로 사용하며 요청에 임의 source/text를 받지 않는다. LearningExplanation과 kind/resource_type=learning_explanation Job을 한 트랜잭션에서 생성하고202 Accepted.job.resource_id로 설명ID를 제공한다. 같은 멱등키는 같은 응답/job을 재생하고 다른 본문은409. owner+concept_version_id+explanation_language+model_id fingerprint의 활성 작업 중복은409 JOB_IN_PROGRESS. 성공한 결과 조회는 무료 AI 미호출이며 별도 멱등키의 생성은 새로운 유료 provider 호출이 될 수 있다. queued/running은 status=generating, succeeded는ready, 확정실패는failed, 호출결과불명은outcome_unknown. ready 이전 content/generated_at은null이며 실패 원인은 latest_job_id로 조회한다. 검증된 본문·근거·ready와 job.succeeded를 fencing+latest pointer 검사 후 원자 저장한다. INVALID_OUTPUT이면 부분 설명을 저장/공개하지 않는다. retry/restart는 기존 /jobs 계약을 따르며 같은 고정버전/언어/모델/입력으로 같은 explanation을 복구한다. 성공 설명을 덮어쓰거나 개념요약·Teach-back·학습완료·coverage를 변경하지 않는다.

#### `GET /learning-explanations` — `listLearningExplanations`

본인 concept_version_id 필수. 타인/없는 버전404; 본인 버전의 모든 생성 상태를 created_at DESC,id DESC로 cursor 페이지 조회한다. 새 개념 버전으로 자동 대체하지 않는다. 실패 기록도 재진입 가능하며 빈 목록은data=[],next_cursor=null. archived 자료의 기존 설명도 본인만 조회 가능. 조회는 AI/BYOK 호출 없음.

#### `GET /learning-explanations/{explanationId}` — `getLearningExplanation`

explanation→user와 고정 version→concept→material 소유권을 검사한다. 타인/없는 ID404. 생성 중/실패/결과불명도200으로 상태와 latest_job_id를 반환하며 content/generated_at은null; ready일 때만 저장된 설명과 자료 근거를 반환한다. archived 자료의 과거 결과는 읽을 수 있다. 최신 concept summary로 대체하지 않으며 GET은 AI 호출/상태변경/학습완료 처리를 하지 않는다.

## DTO / 상세 필드와 오류 응답

전체 요청·응답 JSON Schema, DTO 필드 제약, 예제, 상태 코드, 보안 정의는 동봉된 [`openapi-full.json`](openapi-full.json)의 `components.schemas`, `paths`를 기준으로 확인하세요. 이 JSON은 원본 OpenAPI 3.1.0 계약을 보존합니다.

## 구현 점검 시 유의

- `[x]` 또는 완료 표시를 붙이지 않았습니다. 이 문서의 엔드포인트 존재는 구현 완료를 뜻하지 않습니다.
- A/B 경계의 공유 DTO와 DB 테이블, Job 상태 전이, worker 결과 반영은 팀 통합 시 함께 대조해야 합니다.
- 구독 가격과 결제 API는 계약 범위에서 제외되어 있으며, capability 조회만 있습니다.
