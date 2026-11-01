# N-series review corrections

Source: [Notion correction requests](https://app.notion.com/p/40a5df12680381fd9497d29c0b025ea6), reviewed 2026-11-01. Baseline `ea437de4f8f4e7e97ac45068384a6d9488d536d1`. N10 (idempotency for concept confirmation PATCH) is explicitly reserved for the user's integration work; its API contract is unchanged.

| Request | Implementation and verification |
| --- | --- |
| N1 | The Anthropic adapter states only that it uses the supported `x-api-key` header. The current official authentication page also documents Bearer support; no claim that every previous request returned 401 is made. |
| N3 | Generation has a 300-second total deadline; non-generating key validation has 15 seconds. Output caps are analysis 16,384, questions 12,288, and learning explanations 4,096 tokens, each bounded by the explicitly configured global cap. Worker construction requires lease > generation + preparation budgets; defaults are 420 > 300 + 60 seconds. Preparation expiry is checked before durable dispatch, then the lease is renewed. These limits reduce avoidable timeout failures but cannot guarantee completion by a remote model. |
| N4 | Uploads stage in a private sibling file, handle partial writes, flush/fsync, then publish exclusively with a hard link. Only publication and final ownership/state checks occur inside the DB transaction. Failed writes cannot create a partial final object; simultaneous publication has one winner. Fault injection covers write, flush/fsync, publication, cleanup, and concurrent uploads. |
| N5 | Key validation binds the request digest to a domain-separated server HMAC of owner/model/credential. Neither the credential nor a plain key hash is stored. Identical requests replay; a changed key conflicts. A short committed claim precedes provider I/O, and a random claim token fences completion and release. Abandoned claims expire after 120 seconds; completed responses retain the existing 24-hour window. |
| N6 | Each worker iteration isolates deletion and job failures, emits fixed diagnostic text, backs off, and exits with failure after five consecutive failed iterations. Durable dispatch/recovery semantics still prevent automatic repetition of an uncertain generation call. One-shot execution reports a failed iteration explicitly. |
| N7 | Material completion parses outside its write transaction, then rechecks the material snapshot. Analysis uses snapshot / external preparation / apply / persist-prepared phases, with resource and lease fencing before dispatch and persistence. Key validation holds no connection during HTTP. Job polling uses an owner read and conditional timestamp update. Real HTTP probes and MySQL CI exercise these boundaries. |
| N8 | Provider-specific image preflight runs before dispatch; oversized images are rejected before completion enqueues analysis. The Claude API's documented limit is 10 MB of base64 image data, implemented conservatively as 10,000,000 bytes, with each image dimension at most 8,000 pixels (checked from headers without pixel decoding); the OpenAI path retains the application upload limit of 20 MiB decoded. Ambiguous 503/529/5xx responses remain outcome_unknown. See the contract limitation below for 400/413. |
| N9 | A received result discarded because its resource is stale is a definitive non-retryable INVALID_OUTPUT, rather than an unknown provider outcome. Known rollback deletes newly staged credentials; uncertain COMMIT acknowledgement preserves them within the existing vault TTL. Retry retires the old credential only after commit. Material deletion removes source content while retaining redacted terminal Job audit rows and predecessor links. |

## Contract and operational limits

- The Job error enum has no provider-input-specific value. HTTP 400/413 and local provider input rejection use non-retryable `PROVIDER_UNAVAILABLE`; a more specific public code requires an agreed contract/DDL update. This is a remaining diagnostic limitation, not permission to retry ambiguous failures.
- The official provider error pages describe overload/retry guidance but do not establish that every 503/529 response is unprocessed and unbilled. The conservative outcome-unknown policy is retained.
- Retained deletion audit contains owner/resource/job IDs, model, status, attempts, fingerprints and timing, not source payload, result URLs or usable credential references. It is redacted audit data, not anonymous data. Retention duration and account-deletion policy still require a product decision; no new permanent secret storage is introduced.
- Current Anthropic documentation requires a workspace header for keys that are not scoped to a workspace. The current BYOK contract has no workspace-ID field. Such keys may be rejected until the integration contract supports that context; no workspace identifier is guessed.
- The shared A handler contract still permits SQL-only `prepare(session, job)`. Handlers with slow storage/network work must implement the split preparation interface; `prepare_snapshot`, `apply_prepared`, and `persist_prepared` must be short DB operations.
- Temporary file cleanup can be delayed by OS denial or abrupt process termination. Unpublished staging files are inaccessible through signed storage URLs. A periodic orphan-file cleanup policy is separate operational work.

## Official sources

- [Anthropic authentication](https://platform.claude.com/docs/en/manage-claude/authentication)
- [Anthropic image inputs](https://platform.claude.com/docs/en/build-with-claude/vision)
- [Anthropic API errors](https://platform.claude.com/docs/en/api/errors)
- [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision)
- [OpenAI API errors](https://developers.openai.com/api/docs/guides/error-codes)

## Reproduction

```sh
python -m ruff check app tests scripts
python -m ruff format --check app tests scripts
python -m pytest -q --junitxml=test-results/local.xml
# CI supplies an empty disposable MySQL 8 database and applies migrations 001–004:
python -m pytest -q tests/test_mysql.py --junitxml=test-results/mysql.xml
```

The tests use synthetic credentials, an isolated database/storage directory and deterministic provider responses. No desktop key or paid provider call is used. Windows local tests require an execution environment that permits loopback sockets and hard-link creation. Final command results and the exact pushed SHA's CI status are reported in the completion message and `VALIDATION.md`.
