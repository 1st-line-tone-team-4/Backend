# B schema migration boundary

`Database.create_schema()` creates B tables plus minimal `users` and
`plan_tasks` references only for disposable SQLite development and tests. It is
not a production migration and must not be run against an A database.

`001_mysql_b_baseline.sql` is a new-schema MySQL 8.0.16+ InnoDB baseline for
B tables, generated from the B ORM schema. It deliberately does not create A's
`users` or `plan_tasks`; both must already exist with compatible `CHAR(36)` IDs,
UTF8MB4 binary collation, indexes, and InnoDB foreign key support. The existing
A identity schema must also supply `users.auth_issuer` and `users.auth_subject`
for JWT mapping. Review and reconcile the actual A schema first. `ai_jobs` is a
shared job table in the proposal; if it already exists, do not run the baseline
against it: reconcile its full schema and constraints in an environment-specific
migration. Never overwrite existing A tables.

`002_mysql_job_operational_columns.sql` adds the worker's three internal columns
after the baseline. They track dispatch, an opaque credential reference, and the
last poll; no raw credentials are stored. Do not apply it twice or blindly apply
it to an existing shared jobs table. MySQL DDL can implicitly commit; back up and
prepare a tested recovery plan before any rollout. Migration ordering is 001, 002, 003, then 004. The third migration enforces one
queued or running job per resource type and ID, independent of fingerprint. This repository does not provide an automatic production migration runner.

The baseline uses `utf8mb4_bin` for case-sensitive strings, with `utf8mb4_0900_bin`
(no padding) on idempotency keys, `DATETIME(3)`, checks,
and `LONGTEXT` where the proposal specifies it. MySQL deployment requires an
explicit review of collation compatibility, existing A
foreign key types and collations, server version and SQL mode, generated-column
and check behavior, migration/rollback, concurrency and indexes. CI executes the
migrations and HTTP pipeline on a disposable MySQL 8.0 service. This does not
verify the real A schema, production rollout, or all MySQL concurrency races.

The default task gateway's attempt reader returns an empty dictionary to mean no
attempts are known to standalone B. It does not establish authoritative A
progress. Once A is connected, replace it with an owner- and set-scoped adapter
returning each question's latest attempt and whether any attempt was graded.

`004_mysql_fix_final_checks.sql` adds the CHECK constraints from the team's
`erd-fix-final.sql` (F4) for `materials.mime_type` and `ai_jobs.error_code`, and
the contract's `DEFAULT FALSE` on `materials.archived`. It fails on existing rows
that violate the checks; inspect data before applying it to a non-empty database.
