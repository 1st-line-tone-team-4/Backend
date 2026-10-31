-- Enforce one active job per logical resource, independent of fingerprint.
ALTER TABLE `ai_jobs`
  ADD COLUMN `active_resource_id` CHAR(36)
    GENERATED ALWAYS AS (CASE WHEN `status` IN ('queued','running') THEN `resource_id` ELSE NULL END) STORED,
  ADD CONSTRAINT `uq_ai_jobs_active_resource` UNIQUE (`resource_type`, `active_resource_id`);
