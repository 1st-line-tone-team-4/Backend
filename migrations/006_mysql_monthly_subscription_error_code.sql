-- Apply after 005. Contract 1.4.0-monthly-subscription removes learner-supplied
-- AI keys (BYOK) and renames the job error code CREDENTIAL_EXPIRED to
-- CREDENTIAL_UNAVAILABLE (service-managed provider credential missing).
-- Existing failed jobs keep their history under the new code.
ALTER TABLE `ai_jobs` DROP CHECK `ck_ai_jobs_error_code`;
UPDATE `ai_jobs` SET `error_code` = 'CREDENTIAL_UNAVAILABLE' WHERE `error_code` = 'CREDENTIAL_EXPIRED';
ALTER TABLE `ai_jobs`
  ADD CONSTRAINT `ck_ai_jobs_error_code`
    CHECK (`error_code` IS NULL OR `error_code` IN ('CREDENTIAL_UNAVAILABLE','PROVIDER_AUTH','PROVIDER_RATE_LIMIT','PROVIDER_UNAVAILABLE','INVALID_OUTPUT','TIMEOUT_UNKNOWN','INTERNAL_ERROR'));
