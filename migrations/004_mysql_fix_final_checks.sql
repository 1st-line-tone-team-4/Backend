-- Apply after 003. Aligns B tables with erd-fix-final.sql (F4) and the 1.3.0
-- contract default for materials.archived. Fails if existing rows violate a
-- check; inspect and clean data first on any non-empty database.
ALTER TABLE `materials`
  ALTER COLUMN `archived` SET DEFAULT FALSE,
  ADD CONSTRAINT `ck_materials_mime_type`
    CHECK (`mime_type` IN ('application/pdf','text/plain','image/png','image/jpeg'));
ALTER TABLE `ai_jobs`
  ADD CONSTRAINT `ck_ai_jobs_error_code`
    CHECK (`error_code` IS NULL OR `error_code` IN ('CREDENTIAL_EXPIRED','PROVIDER_AUTH','PROVIDER_RATE_LIMIT','PROVIDER_UNAVAILABLE','INVALID_OUTPUT','TIMEOUT_UNKNOWN','INTERNAL_ERROR'));
