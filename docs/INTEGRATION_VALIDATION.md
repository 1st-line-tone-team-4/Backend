# A+B integration verification

Integration branch: `integration/a-b-20261101`.

Preserved inputs: B `4e6752fb40e759774221520a2e28ef5f7f13c19a` and A `7d237ae8c3ad911eb55cc3dd05565376de79c01d`. Their original branches and authorship remain intact. This branch joins the implementations; it does not deploy or merge into main.

## Implemented integration

- One FastAPI factory, SQLAlchemy metadata, JWT identity mapping, durable job engine and credential vault. The old A app factory delegates to the integrated runtime.
- Canonical 35-table schema and normalized A records. SQL migrations 000–005 support an empty database or the documented B004 upgrade preconditions. The experimental A Alembic schema is retained as history and cannot be applied accidentally.
- Goal, plan, Today, attempt/grading, Teach-back, report/feedback and record routes use actual B materials, immutable concept versions, generated questions and evidence. The production default task gateway reads canonical plan progress; question generation and independent explanations never complete tasks.
- The published contract includes 40 paths / 51 operations: B 24 and A 27. Sharing/publication creation, score claims and billing operations remain unimplemented. Private storage transport and health routes are separate from that count.
- JWTs are verified against an externally configured RS256 issuer/audience. First authenticated use provisions the user. This repository does not implement password registration, token issuance or refresh.

## Review fixes

Independent source review and regressions cover successful grade persistence, exactly-once progress, old versus active plan revisions, MySQL millisecond attempt ordering, current-read plan rescheduling, sequence-based conversation pagination, pinned evidence without an optional quote, SQL-compatible HALF_UP report rounding, public-history answer allowlists, and owner-bound cursors.

Standalone session creation now locks and rechecks its source material/version against concurrent deletion. A 1 MiB UTF-8 JSON text-prompt limit rejects oversized context before provider dispatch without truncating conversation history. Images retain separate limits. Invalid optional JWT email claims become null rather than producing an invalid profile response.

## Reproducible validation

Final local Windows/Python 3.12 run: **539 passed, 1 skipped** in 171.33s. The skip is the explicitly unavailable disposable MySQL URL. Ruff lint/format, compilation and whitespace checks passed. The only warning is the existing Starlette/httpx test-client deprecation.

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
- No static type checker is configured in this repository. Compilation, Ruff, schema/contract checks and runtime tests are recorded separately.

The earlier B-only results in [VALIDATION.md](VALIDATION.md) are historical; they do not substitute for the integration branch's checks.
