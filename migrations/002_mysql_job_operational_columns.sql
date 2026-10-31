-- Apply after 001, or review against an existing compatible ai_jobs table.
ALTER TABLE `ai_jobs`
  ADD COLUMN `dispatched_at` DATETIME(3) NULL,
  ADD COLUMN `credential_ref` VARCHAR(64) NULL,
  ADD COLUMN `last_polled_at` DATETIME(3) NULL;
