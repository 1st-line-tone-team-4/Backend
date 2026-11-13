# A+B integration verification

> **역사적 검증 기록:** 이 문서는 통합 초기의 1.3 계약과 당시 미구현 경로를 기록합니다. 이후 월 구독형 1.4 계약에서 `GET /billing/capabilities`를 추가하고 `POST /ai/validate-key`를 제거했습니다. 현재 API 목록과 동작은 [API_SPEC_FULL.md](API_SPEC_FULL.md) 및 [openapi-1.4.0-monthly-subscription.json](../contracts/openapi-1.4.0-monthly-subscription.json)을 기준으로 확인하세요.

Integration branch: `integration/a-b-20261101`.

Preserved inputs: B `4e6752fb40e759774221520a2e28ef5f7f13c19a` and A `7d237ae8c3ad911eb55cc3dd05565376de79c01d`. Their original branches and authorship remain intact. This branch joins the implementations; it does not deploy or merge into main.

## Implemented integration

- One FastAPI factory, SQLAlchemy metadata, JWT identity mapping, durable job engine and credential vault. The old A app factory delegates to the integrated runtime.
- Canonical 35-table schema and normalized A records. SQL migrations 000–005 support an empty database or the documented B004 upgrade preconditions. The experimental A Alembic schema is retained as history and cannot be applied accidentally.
- Goal, plan, Today, attempt/grading, Teach-back, report/feedback and record routes use actual B materials, immutable concept versions, generated questions and evidence. The production default task gateway reads canonical plan progress; question generation and independent explanations never complete tasks.
- The published contract includes 40 paths / 51 operations: B 24 and A 27. Sharing/publication creation, score claims and billing operations remain unimplemented. Private storage transport and health routes are separate from that count.
- JWTs are verified against an externally configured RS256 issuer/audience. First authenticated use provisions the user. This repository does not implement password registration, token issuance or refresh.

## Contract coverage and timing (N15)

The full repository contract has **50 paths / 64 operations**. The integrated
server publishes **40 paths / 51 operations** (B24 + A27). The following 13
contract operations have no implemented HTTP handler registered in this server;
they are not completed features hidden only by the OpenAPI filter. Comparing
the canonical operations, FastAPI's resolved mounted-route inventory and source
route declarations found no implemented contract handler left unregistered.
Health and signed private-storage transport are outside the 51-operation count.

All paths below are relative to `/v1`.

| Method | Missing path | Contract operationId |
| --- | --- | --- |
| GET | `/billing/capabilities` | `getBillingCapabilities` |
| GET | `/publications` | `listRecommendedQuestions` |
| POST | `/publications` | `publishQuestion` |
| GET | `/publications/{publicationId}` | `getPublicQuestion` |
| DELETE | `/publications/{publicationId}` | `withdrawQuestion` |
| POST | `/publications/{publicationId}/attempts` | `submitPublicAttempt` |
| GET | `/score-claims` | `listMyScoreClaims` |
| POST | `/score-claims` | `createScoreClaim` |
| DELETE | `/score-claims/{claimId}` | `withdrawScoreClaim` |
| POST | `/score-claims/{claimId}/submit` | `submitScoreClaim` |
| GET | `/review/score-claims` | `listScoreReviewQueue` |
| PATCH | `/review/score-claims/{claimId}` | `reviewScoreClaim` |
| GET | `/review/score-claims/{claimId}/proof` | `getScoreProof` |

`GET /records/public-attempts` **is implemented**. It reads the authenticated
owner's stored snapshots, including legitimate seeded/imported records, and
need not always return an empty list. This read does not implement publication
creation or new public-attempt submission. The 35-table schema likewise does
not establish that these 13 business operations exist.

The old `x-contract-rules.jobs` sentence saying a provider timeout of 60 seconds
was corrected in the repository contract to match the already-approved runtime:

| Timing boundary | Runtime value |
| --- | --- |
| Generating provider request | 300-second total deadline |
| Non-generating key validation | 15-second total deadline |
| Preparation budget | 60 seconds |
| Default worker lease | 420 seconds |
| Constructor invariant | `lease > generation + preparation`, hence `420 > 300 + 60` |

Evidence is `ProviderRegistry.GENERATION_TIMEOUT_SECONDS` /
`KEY_VALIDATION_TIMEOUT_SECONDS` in `app/providers.py` and `JobService.__init__`
in `app/jobs.py`. These are request budgets and lease safety checks, not a
guarantee of total queue-to-result latency. This N15 change corrects prose only:
schemas, statuses, queue/credential TTL, retry/restart and unknown-outcome policy
are unchanged. No external Notion source was rewritten. The two OpenAPI docs
snapshots are regenerated using the existing exporter; the runtime intentionally
omits `x-contract-rules`, so this prose correction does not add that extension to
the published documents.

## Review fixes

Independent source review and regressions cover successful grade persistence, exactly-once progress, old versus active plan revisions, MySQL millisecond attempt ordering, current-read plan rescheduling, sequence-based conversation pagination, pinned evidence without an optional quote, SQL-compatible HALF_UP report rounding, public-history answer allowlists, and owner-bound cursors.

Standalone session creation now locks and rechecks its source material/version against concurrent deletion. A 1 MiB UTF-8 JSON text-prompt limit rejects oversized context before provider dispatch without truncating conversation history. Images retain separate limits. Invalid optional JWT email claims become null rather than producing an invalid profile response.

## Reproducible validation

### N11/N14 and structural follow-up

N11 was reproduced before correction in three signed-JWT loopback HTTP tests.
Only queued/running jobs now count as busy. Unknown job history, same-answer
duplicate-cost guards, explicit restart acknowledgment, current-resource checks
and stale-worker fencing remain. Tests cover changed-answer recovery,
abandon/new-session/skip/reschedule, new-message continuation, no automatic
redispatch and the then-current report-pending policy (superseded by N12 below). MySQL helpers additionally
exercise stale repeatable-read snapshots and a worker-held latest-job row.

N14 batches plan display pins and progress. Authoritative rescheduling retains
goal/plan/task locks and raw current reads; it does not replace those reads with
snapshot aggregates. Measured SELECT counts exclude the unchanged JWT user
lookup and fixture setup:

| Path / fixture | Before | After |
| --- | ---: | ---: |
| Plan detail, 300 tasks | 503 | 6 |
| Plan detail, 6000 tasks | Not measured | 6 |
| Today, 300 tasks | 504 | 7 |
| Plan list, 30 plans | 61 | 3 |
| Create plan, 300 tasks | 507 | 10 |
| Reschedule, 300 tasks | 1010 | 18 |
| Question-set projection, 20 questions, no task gateway | 102 | 5 |

These are query-count regressions, not production latency/load benchmarks.
Three cursor codecs now share `app/pagination.py` while retaining their existing
wire formats; four literal cursor fixtures and six stored request digests pin
compatibility. Request envelopes and strict JSON parsing share neutral helpers.
`app/projections.py` consolidates question/evidence/attempt projections and
removes the gateway-to-router dependency cycle. Public integration import paths
remain aliases. Only graded results expose correct answers, and child evidence queries
are batched without moving ownership checks out of callers.

The complete module map, request flow and remaining query hot spots are in
[ARCHITECTURE.md](ARCHITECTURE.md). This refactor does not implement the 13 missing
operations. N12/N13 were subsequently decided by the user as recorded below;
N10 remains reserved.

### Confirmed N12/N13 policies

The user confirmed that terminal report work may be abandoned while preserving
existing diagnostics, and that the app's monthly subscription policy does not
require a new per-generation acknowledgment for independent learning explanations.

- N12 changes `PATCH /teachback-sessions/{sessionId}` behavior, not its schema:
  active sessions without queued/running jobs remain abandonable. A
  `report_pending` session is also eligible when its current owner-bound report
  job is terminal and no report is stored. Completed sessions and stored reports
  stay protected. Repeating abandonment returns the current DTO with `200`, even
  after its plan becomes inactive or its task is skipped. Only session status is
  changed; messages, latest-job pointer, terminal job diagnostics and progress
  history remain. No extra debug payloads or secrets are persisted. Late worker
  results and retry/restart after abandonment remain fenced/stale. Existing
  material reference and deletion rules still apply.
- N13 adds no consent field or new charge gate to `ExplanationInput`. A fresh
  idempotency key is an explicit new-generation request, including after an
  unknown outcome. Same-key replay, active-job duplicate protection and no
  automatic redispatch remain independent rules. BYOK provider charges are
  separate from app subscriptions; this decision does not remove common restart
  acknowledgment, question-generation R1 or public-sharing consent. It does not
  implement billing/subscription entitlement APIs.
- N13 retains the existing active-job check and unique active-fingerprint
  constraint. Tests assert committed job/resource counts, rejected-request
  rollback and provider call counts. No new locking-read optimization or
  fingerprint format change is included in the policy update.

Canonical request/response schemas, database schema and the **51/64 operation**
inventory are unchanged. Canonical descriptions and both published OpenAPI
snapshots document the new N12 transition. The final local N12/N13 suite passed
**594 tests, 1 skipped** in **208.33s** on Windows/Python 3.12. Ruff lint/format
(83 files), compilation and whitespace checks passed. The skip is the unavailable
local disposable MySQL URL; the one warning is the existing Starlette/httpx
test-client deprecation. Independent source review checked the final reduced
delta and corrected the N12 retry/restart error-precedence prose.

The CI MySQL test includes the new report-abandon current-read/NOWAIT and both
restart interleavings, plus explanation same-input old-snapshot and distinct-source
concurrency outcomes. Those assertions include durable rows, replay/rollback,
opaque credential-reference cleanup and no provider dispatch during acceptance.
The exact pushed commit's GitHub Actions result remains the evidence for actual
MySQL execution. Earlier results below are historical evidence for the preceding
structural revision.

### Earlier structural revision (`d4c43ac`) and original baseline

Previously recorded baseline Windows/Python 3.12 run, before the N11–N15 follow-up: **539 passed, 1 skipped** in 171.33s. The skip is the explicitly unavailable disposable MySQL URL. Baseline Ruff lint/format, compilation and whitespace checks passed. The only warning is the existing Starlette/httpx test-client deprecation.

The structural follow-up Windows/Python 3.12 full suite passed **569 tests, 1 skipped** in
188.16s. A final test-only ordering case was added after that run's collection;
the complete projection test module then passed **4 tests**, including that new
case. No runtime code changed between these runs. Ruff lint/format (81 files),
compilation and whitespace checks passed. The skip remains the unavailable local
MySQL URL; the only warning remains Starlette/httpx deprecation. A first run
found two test fault-injection references to a removed private wrapper; those
references were updated to the actual shared validation boundary, without
weakening their stale-state/rollback/provider-call assertions, before the
successful full run.

An independent source reviewer checked the final runtime snapshot, including
MySQL lock ordering, ownership, result redaction and cost/fencing rules. A separate
read-only comparison against the pre-refactor implementations passed 83 cursor,
request-envelope and digest assertions. These checks support the reviewed paths;
they do not establish the absence of all defects or resolve the policy decisions
below. The exact pushed commit's GitHub Actions result is the source of truth for
the final full CI suite and actual disposable MySQL run. N15 route/schema/prose
checks and the historical baseline are not substitutes for that run.

From the repository root after the documented editable install:

```sh
python -m ruff check app src tests scripts
python -m ruff format --check app src tests scripts
python -m compileall -q app src
python -m pytest -q --junitxml=test-results/integration-local.xml
```

`tests/test_ab_e2e.py` starts a real loopback HTTP server with real signed JWT validation and the canonical task gateway. It runs upload → analysis → confirmation → goal/plan/Today → five graded answers → independent explanation → three Teach-back rounds → final answer/report → records/actions. It checks duplicate requests, resumed reads, immutable reports, ownership isolation and secret retirement. Only AI responses are injected. Existing B tests additionally run a separate worker process.

`tests/test_ab_auth_cursor.py` exercises real RSA signatures, concurrent first-login provisioning, database-collation identity aliases, optional claims, and cursor signatures/scopes/types. Domain tests cover failures, malformed results, retry/restart, rollback and fencing rather than only happy responses.

The CI workflow runs the full SQLite suite and then `tests/test_mysql.py` against an empty disposable MySQL 8 service. MySQL verification applies all six migrations, refuses nonempty ID-only task stubs before persistent DDL, preserves preexisting B rows, executes the same signed-JWT HTTP learning flow and checks concurrent claims/current reads/NOWAIT rescheduling. A local run without `TEST_MYSQL_URL` explicitly skips this test; SQLite is not evidence of MySQL locking correctness. Inspect the workflow run for the exact integration commit before merging.

## Limits and team decisions

- No real provider call or user API-key read was performed for this integration. Model quality, external authentication-provider onboarding, production deployment/load, OCR accuracy and operational recovery remain outside this test evidence.
- The frontend repository had no branch refs or code when checked. No frontend was invented, and no browser/UI integration is claimed. Cross-origin deployment still needs an agreed origin/CORS configuration.
- Existing noncanonical A databases and nonempty B task stubs require a separately reviewed data reconciliation/backfill. Migration 005 does not prove every preexisting users index/check; operators must verify its documented canonical prerequisites.
- N10 concept-confirmation PATCH idempotency remains reserved for the user's integration decision. Shared error-enum expansion, audit-retention duration and provider workspace-key policy are not silently changed here.
- N12/N13 are confirmed and implemented as described above. Removing BYOK or the global restart/R1 safeguards would be a separate decision; no such removal is implied by the monthly subscription policy.
- No static type checker is configured in this repository. Compilation, Ruff, schema/contract checks and runtime tests are recorded separately.

The original attached review file was not available from the checked source references. Follow-up findings were independently reproduced from repository code/contracts; this record does not claim that the original attachment was inspected or that every pending policy issue has been resolved.

The earlier B-only results in [VALIDATION.md](VALIDATION.md) are historical; they do not substitute for the integration branch's checks.
