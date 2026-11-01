# A+B integration verification

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

Previously recorded baseline Windows/Python 3.12 run, before the N11–N15 follow-up: **539 passed, 1 skipped** in 171.33s. The skip is the explicitly unavailable disposable MySQL URL. Baseline Ruff lint/format, compilation and whitespace checks passed. The only warning is the existing Starlette/httpx test-client deprecation.

The final follow-up test counts, exact commit SHA and CI run are pending integration verification. The baseline result above must not be presented as the result for the updated working tree. N15 documentation checks establish the route inventory and prose/schema diff; they do not substitute for the final full-suite or MySQL run.

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
- N12 remains a contract decision: a failed report currently leaves its session `report_pending`, while abandon PATCH is limited to active sessions. Allowing abandonment after failed/unknown report jobs only when no queued/running job exists is a recommendation, not implemented or silently adopted policy.
- N13 remains a contract decision: independent explanation creation explicitly permits a new generation with a new idempotency key and has no duplicate-cost acknowledgment field. Blocking a matching `outcome_unknown` generation with `409` and directing the user to the existing acknowledged restart is a recommendation that would change contract behavior, not a completed fix.
- No static type checker is configured in this repository. Compilation, Ruff, schema/contract checks and runtime tests are recorded separately.

The original attached review file was not available from the checked source references. Follow-up findings were independently reproduced from repository code/contracts; this record does not claim that the original attachment was inspected or that every pending policy issue has been resolved.

The earlier B-only results in [VALIDATION.md](VALIDATION.md) are historical; they do not substitute for the integration branch's checks.
