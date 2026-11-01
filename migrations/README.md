# Canonical A+B schema migrations

`app.models.Base` is the single ORM registry for all 35 contract tables. The
`language_learning_api.models` module only reexports those classes. IDs remain
`CHAR(36)` in MySQL and strings in API/domain code. `Database.create_schema()` is
for disposable development/tests; it creates the full A+B schema and is not an
upgrade mechanism.

## Active MySQL chain

Use MySQL 8.0.16+ with enforced CHECK constraints, InnoDB and UTF8MB4. Execute
each file once, in order, stopping at the first error. Do not use a client option
that continues after SQL errors. No automatic production runner is provided.

- **New, empty database:** `000_mysql_shared_prerequisites.sql`, then 001–005.
  000 creates canonical `users` and an empty `plan_tasks(id)` prerequisite. 005
  expands that task table in place and creates the remaining A tables.
- **Existing B database already at 004:** apply 005 only after verifying the
  prerequisites below. Do not rerun 000 or the B baseline.
- **Existing independent A/other database:** this chain is not a compatible
  upgrade. Inventory and reconcile its data in a separately reviewed migration.
  In particular, do not reinterpret `units`, `jobs`, `idempotency_records`, UUID
  hex IDs, JSON task pins, or JSON report items as the canonical shared schema.

001 is the original B baseline and deliberately excludes `users`/`plan_tasks`.
002 adds dispatch/credential-reference/poll timestamps; no raw credential is
stored. 003 prevents multiple active jobs for a resource. 004 applies the final
MIME/error checks and the material archive default. 005 adds normalized goals,
plans/pins, attempts, sessions/messages, reports/items/evidence, actions and
feedback. Publication/public-attempt/score-claim tables complete the contract's
schema; this migration does not enable sharing, score or billing business routes.

## 005 preflight and task-stub boundary

Stop all writers and take a backup before migration. MySQL DDL implicitly commits;
schema changes are not transactionally rolled back. Check server version, SQL
mode, FK types/collations and existing data first, and test recovery on a copy.

005 expects the known B 004 schema, a canonical seven-column `users` table and an
**empty, ID-only `plan_tasks` stub** with binary-collated CHAR(36) IDs. Its temporary
primary-key guard rejects nonempty stubs, incompatible identity/task columns or
existing A-domain tables before any persistent table change. The client must stop
on that error. Disconnecting removes the temporary guard. It never deletes or
recreates the task table, so existing B foreign-key definitions remain attached.

The guard checks column shape, not every existing index or CHECK. Before an
existing-B upgrade, separately verify `users` has `PRIMARY KEY (id)`,
`UNIQUE (auth_issuer, auth_subject)` and `CHECK (timezone = 'Asia/Seoul')`, together
with the contract's nullability, types and collations. Fresh 000 supplies these
constraints. Passing the 005 guard does not establish full compatibility of an
arbitrary preexisting identity schema; reconcile missing constraints/data in a
separately reviewed migration before using this chain.

A nonempty task stub has no recorded plan, goal, due date, task type or pinned
concept provenance. There is no safe automatic backfill from an ID alone. Such a
database requires a separately reviewed, authoritative data migration; do not
delete task rows, fabricate history, or disable the guard. This repository has
not inspected or changed any production/independent A database.

On a partial DDL failure, stop and inspect the schema against the backup. 005 is
intentionally nonrepeatable; it rejects partially-created A tables rather than
silently claiming success. Restoration or an explicitly reviewed forward repair
must precede a retry.

## Constraint representation and verification

The canonical contract is `contracts/schema-proposal-1.3.0.sql`. The ORM uses
VARCHAR plus CHECK for its enum domains; conversion to native MySQL ENUM remains
a separate policy decision. Dates use UTC `DATETIME(3)`; IDs and normal strings
use `utf8mb4_bin`. Idempotency keys use no-padding `utf8mb4_0900_bin`. Report
scores use DECIMAL; multiplying the coverage numerator by `1.0` preserves the
contract formula while avoiding SQLite integer division during isolated tests.
Ownership and cross-row business invariants described in the contract remain
transactional application checks; foreign keys alone do not establish them.

The schema tests compare all contract columns, primary keys, foreign keys,
unique constraints and checks. Disposable MySQL CI executes 000–005 and the real
HTTP pipeline. This is evidence for the tested version/configuration, not a
claim that a production rollout, recovery exercise or every concurrency race
has been validated.

## Quarantined experimental Alembic history

`legacy_a_experimental/` preserves the original independent A revisions for
provenance. They declared incompatible duplicate shared tables and are **not**
an active migration path. `migrations/env.py` fails explicitly on Alembic
execution; no automatic stamp or conversion is supplied. Do not move those
revisions back into the active path or run `alembic upgrade head` on this schema.
