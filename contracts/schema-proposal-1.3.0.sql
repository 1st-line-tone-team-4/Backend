-- 1호선톤 1.3.0-backend-proposal | 2026-10-31
-- NEW schema only, NOT a migration. Never executed against any DB.
-- Backend recommendation: MySQL 8.0.16+ InnoDB; actual deployment engine unverified.
-- UTF8, UUID v4, UTC DATETIME(3); app transaction checks documented below.
-- daily_minutes/proficiency_level intentionally absent. No credit ledger/billing writes.
-- AI keys NEVER enter these tables: separate encrypted TTL store <=15 minutes.

CREATE TABLE `users` (
  `id` CHAR(36) NOT NULL,
  `auth_issuer` VARCHAR(255) NOT NULL,
  `auth_subject` VARCHAR(255) NOT NULL,
  `email` VARCHAR(320) NULL,
  `display_name` VARCHAR(120) NULL,
  `timezone` VARCHAR(64) NOT NULL DEFAULT 'Asia/Seoul',
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_users_0` (`auth_issuer`, `auth_subject`),
  CONSTRAINT `ck_users_0` CHECK (timezone = 'Asia/Seoul')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `user_subscriptions` (
  `user_id` CHAR(36) NOT NULL,
  `status` VARCHAR(32) NOT NULL DEFAULT 'pricing_pending',
  `updated_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`user_id`),
  CONSTRAINT `fk_user_subscriptions_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_user_subscriptions_0` CHECK (status IN ('pricing_pending','active'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `ai_jobs` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `kind` ENUM('analysis','questions','grade','message','report','learning_explanation') NOT NULL,
  `status` ENUM('queued','running','succeeded','failed','outcome_unknown') NOT NULL,
  `resource_type` ENUM('material','question_set','attempt','session','learning_explanation') NOT NULL,
  `resource_id` CHAR(36) NOT NULL,
  `request_fingerprint` CHAR(64) NOT NULL,
  `request_payload` JSON NOT NULL,
  `model_id` VARCHAR(100) NOT NULL,
  `attempt_no` SMALLINT NOT NULL,
  `retryable` BOOLEAN NOT NULL,
  `error_code` VARCHAR(64) NULL,
  `result_url` VARCHAR(512) NULL,
  `fencing_token` BIGINT NOT NULL,
  `lease_expires_at` DATETIME(3) NULL,
  `created_at` DATETIME(3) NOT NULL,
  `updated_at` DATETIME(3) NOT NULL,
  `active_fingerprint` CHAR(64) GENERATED ALWAYS AS (CASE WHEN status IN ('queued','running') THEN request_fingerprint ELSE NULL END) STORED,
  `previous_job_id` CHAR(36) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_jobs_0` (`user_id`, `active_fingerprint`),
  UNIQUE KEY `uq_ai_jobs_1` (`previous_job_id`),
  CONSTRAINT `fk_ai_jobs_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_ai_jobs_1` FOREIGN KEY (`previous_job_id`) REFERENCES `ai_jobs` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_ai_jobs_0` CHECK (attempt_no BETWEEN 1 AND 3),
  CONSTRAINT `ck_ai_jobs_1` CHECK (fencing_token >= 0),
  CONSTRAINT `ck_ai_jobs_2` CHECK (status = 'failed' OR retryable = FALSE)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `exam_goals` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `title` VARCHAR(120) NOT NULL,
  `exam_type` ENUM('school_exam','toeic','toefl','entrance_exam','other') NOT NULL,
  `exam_date` DATE NOT NULL,
  `status` ENUM('active','paused','completed') NOT NULL,
  `created_at` DATETIME(3) NOT NULL,
  `updated_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  CONSTRAINT `fk_exam_goals_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `materials` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `filename` VARCHAR(255) NOT NULL,
  `mime_type` VARCHAR(100) NOT NULL,
  `size_bytes` BIGINT NOT NULL,
  `storage_key` VARCHAR(512) NOT NULL,
  `processing_status` ENUM('upload_pending','queued','processing','ready','failed') NOT NULL,
  `page_count` INT NULL,
  `archived` BOOLEAN NOT NULL DEFAULT FALSE,
  `created_at` DATETIME(3) NOT NULL,
  `updated_at` DATETIME(3) NOT NULL,
  `latest_job_id` CHAR(36) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_materials_0` (`storage_key`),
  CONSTRAINT `fk_materials_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_materials_1` FOREIGN KEY (`latest_job_id`) REFERENCES `ai_jobs` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_materials_0` CHECK (size_bytes BETWEEN 1 AND 20971520),
  CONSTRAINT `ck_materials_1` CHECK (page_count IS NULL OR page_count >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `material_units` (
  `id` CHAR(36) NOT NULL,
  `material_id` CHAR(36) NOT NULL,
  `ordinal` INT NOT NULL,
  `title` VARCHAR(200) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_material_units_0` (`material_id`, `ordinal`),
  UNIQUE KEY `uq_material_units_1` (`id`, `material_id`),
  CONSTRAINT `fk_material_units_0` FOREIGN KEY (`material_id`) REFERENCES `materials` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_material_units_0` CHECK (ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `material_chunks` (
  `id` CHAR(36) NOT NULL,
  `material_id` CHAR(36) NOT NULL,
  `page_number` INT NOT NULL,
  `ordinal` INT NOT NULL,
  `content` LONGTEXT NOT NULL,
  `embedding_ref` VARCHAR(512) NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_material_chunks_0` (`material_id`, `ordinal`),
  UNIQUE KEY `uq_material_chunks_1` (`id`, `material_id`),
  CONSTRAINT `fk_material_chunks_0` FOREIGN KEY (`material_id`) REFERENCES `materials` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_material_chunks_0` CHECK (page_number >= 1),
  CONSTRAINT `ck_material_chunks_1` CHECK (ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `concepts` (
  `id` CHAR(36) NOT NULL,
  `material_id` CHAR(36) NOT NULL,
  `unit_id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_concepts_0` (`id`, `material_id`),
  UNIQUE KEY `uq_concepts_1` (`id`, `unit_id`),
  CONSTRAINT `fk_concepts_0` FOREIGN KEY (`material_id`) REFERENCES `materials` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_concepts_1` FOREIGN KEY (`unit_id`, `material_id`) REFERENCES `material_units` (`id`, `material_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `concept_versions` (
  `id` CHAR(36) NOT NULL,
  `concept_id` CHAR(36) NOT NULL,
  `version_no` INT NOT NULL,
  `name` VARCHAR(200) NOT NULL,
  `summary` TEXT NULL,
  `is_confirmed` BOOLEAN NOT NULL,
  `confirmed_at` DATETIME(3) NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_concept_versions_0` (`concept_id`, `version_no`),
  UNIQUE KEY `uq_concept_versions_1` (`id`, `concept_id`),
  CONSTRAINT `fk_concept_versions_0` FOREIGN KEY (`concept_id`) REFERENCES `concepts` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_concept_versions_0` CHECK (version_no >= 1),
  CONSTRAINT `ck_concept_versions_1` CHECK ((is_confirmed = FALSE AND confirmed_at IS NULL) OR (is_confirmed = TRUE AND confirmed_at IS NOT NULL AND summary IS NOT NULL AND CHAR_LENGTH(summary) > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `concept_checklist_items` (
  `id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  `ordinal` INT NOT NULL,
  `criterion` TEXT NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_concept_checklist_items_0` (`concept_version_id`, `ordinal`),
  UNIQUE KEY `uq_concept_checklist_items_1` (`id`, `concept_version_id`),
  CONSTRAINT `fk_concept_checklist_items_0` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_concept_checklist_items_0` CHECK (ordinal BETWEEN 1 AND 50)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `concept_version_evidence` (
  `concept_version_id` CHAR(36) NOT NULL,
  `chunk_id` CHAR(36) NOT NULL,
  `quote` TEXT NULL,
  PRIMARY KEY (`concept_version_id`, `chunk_id`),
  CONSTRAINT `fk_concept_version_evidence_0` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_concept_version_evidence_1` FOREIGN KEY (`chunk_id`) REFERENCES `material_chunks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `learning_explanations` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `concept_id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  `material_id` CHAR(36) NOT NULL,
  `explanation_language` ENUM('ko','en') NOT NULL,
  `status` ENUM('generating','ready','failed','outcome_unknown') NOT NULL,
  `latest_job_id` CHAR(36) NOT NULL,
  `content_title` VARCHAR(200) NULL,
  `content_markdown` LONGTEXT NULL,
  `created_at` DATETIME(3) NOT NULL,
  `generated_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_learning_explanations_0` (`id`, `concept_version_id`),
  UNIQUE KEY `uq_learning_explanations_1` (`latest_job_id`),
  CONSTRAINT `fk_learning_explanations_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_learning_explanations_1` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_learning_explanations_2` FOREIGN KEY (`concept_version_id`, `concept_id`) REFERENCES `concept_versions` (`id`, `concept_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_learning_explanations_3` FOREIGN KEY (`concept_id`, `material_id`) REFERENCES `concepts` (`id`, `material_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_learning_explanations_4` FOREIGN KEY (`latest_job_id`) REFERENCES `ai_jobs` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_learning_explanations_0` CHECK ((status = 'ready' AND content_title IS NOT NULL AND CHAR_LENGTH(content_title) BETWEEN 1 AND 200 AND content_markdown IS NOT NULL AND CHAR_LENGTH(content_markdown) BETWEEN 1 AND 24000 AND generated_at IS NOT NULL) OR (status <> 'ready' AND content_title IS NULL AND content_markdown IS NULL AND generated_at IS NULL)),
  CONSTRAINT `ck_learning_explanations_1` CHECK (generated_at IS NULL OR generated_at >= created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `learning_explanation_evidence` (
  `explanation_id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  `chunk_id` CHAR(36) NOT NULL,
  `excerpt` TEXT NOT NULL,
  PRIMARY KEY (`explanation_id`, `chunk_id`),
  CONSTRAINT `fk_learning_explanation_evidence_0` FOREIGN KEY (`explanation_id`, `concept_version_id`) REFERENCES `learning_explanations` (`id`, `concept_version_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_learning_explanation_evidence_1` FOREIGN KEY (`concept_version_id`, `chunk_id`) REFERENCES `concept_version_evidence` (`concept_version_id`, `chunk_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_learning_explanation_evidence_0` CHECK (CHAR_LENGTH(excerpt) BETWEEN 1 AND 2000)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `study_plans` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `exam_goal_id` CHAR(36) NOT NULL,
  `revision` INT NOT NULL,
  `status` ENUM('active','superseded') NOT NULL,
  `generated_at` DATETIME(3) NOT NULL,
  `active_goal_id` CHAR(36) GENERATED ALWAYS AS (CASE WHEN status = 'active' THEN exam_goal_id ELSE NULL END) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_study_plans_0` (`exam_goal_id`, `revision`),
  UNIQUE KEY `uq_study_plans_1` (`active_goal_id`),
  CONSTRAINT `fk_study_plans_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_study_plans_1` FOREIGN KEY (`exam_goal_id`) REFERENCES `exam_goals` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_study_plans_0` CHECK (revision >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `study_plan_materials` (
  `study_plan_id` CHAR(36) NOT NULL,
  `material_id` CHAR(36) NOT NULL,
  `ordinal` INT NOT NULL,
  PRIMARY KEY (`study_plan_id`, `material_id`),
  UNIQUE KEY `uq_study_plan_materials_0` (`study_plan_id`, `ordinal`),
  CONSTRAINT `fk_study_plan_materials_0` FOREIGN KEY (`study_plan_id`) REFERENCES `study_plans` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_study_plan_materials_1` FOREIGN KEY (`material_id`) REFERENCES `materials` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_study_plan_materials_0` CHECK (ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `plan_tasks` (
  `id` CHAR(36) NOT NULL,
  `study_plan_id` CHAR(36) NOT NULL,
  `ordinal` INT NOT NULL,
  `due_date` DATE NOT NULL,
  `task_type` ENUM('solve','teach_back','review') NOT NULL,
  `target_count` SMALLINT NOT NULL,
  `status` ENUM('pending','in_progress','completed','skipped') NOT NULL,
  `completed_at` DATETIME(3) NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_plan_tasks_0` (`study_plan_id`, `ordinal`),
  CONSTRAINT `fk_plan_tasks_0` FOREIGN KEY (`study_plan_id`) REFERENCES `study_plans` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_plan_tasks_0` CHECK (ordinal >= 1),
  CONSTRAINT `ck_plan_tasks_1` CHECK (target_count BETWEEN 1 AND 20),
  CONSTRAINT `ck_plan_tasks_2` CHECK ((status = 'completed' AND completed_at IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `plan_task_concepts` (
  `task_id` CHAR(36) NOT NULL,
  `concept_id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`task_id`, `concept_id`),
  UNIQUE KEY `uq_plan_task_concepts_0` (`task_id`, `concept_version_id`),
  CONSTRAINT `fk_plan_task_concepts_0` FOREIGN KEY (`task_id`) REFERENCES `plan_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_plan_task_concepts_1` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_plan_task_concepts_2` FOREIGN KEY (`concept_version_id`, `concept_id`) REFERENCES `concept_versions` (`id`, `concept_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `question_sets` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `unit_id` CHAR(36) NOT NULL,
  `task_id` CHAR(36) NULL,
  `difficulty` ENUM('easy','medium','hard') NOT NULL,
  `requested_count` SMALLINT NOT NULL,
  `status` ENUM('generating','ready','failed') NOT NULL,
  `job_id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_question_sets_0` (`job_id`),
  CONSTRAINT `fk_question_sets_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_sets_1` FOREIGN KEY (`unit_id`) REFERENCES `material_units` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_sets_2` FOREIGN KEY (`task_id`) REFERENCES `plan_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_sets_3` FOREIGN KEY (`job_id`) REFERENCES `ai_jobs` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_question_sets_0` CHECK (requested_count BETWEEN 1 AND 20)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `question_set_concepts` (
  `set_id` CHAR(36) NOT NULL,
  `concept_id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`set_id`, `concept_id`),
  UNIQUE KEY `uq_question_set_concepts_0` (`set_id`, `concept_version_id`),
  CONSTRAINT `fk_question_set_concepts_0` FOREIGN KEY (`set_id`) REFERENCES `question_sets` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_set_concepts_1` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_set_concepts_2` FOREIGN KEY (`concept_version_id`, `concept_id`) REFERENCES `concept_versions` (`id`, `concept_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `questions` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `question_type` ENUM('multiple_choice','short_answer') NOT NULL,
  `prompt` LONGTEXT NOT NULL,
  `options` JSON NOT NULL,
  `answer_key` JSON NOT NULL,
  `public_explanation` TEXT NOT NULL,
  `accepted_answers` JSON NOT NULL,
  `difficulty` ENUM('easy','medium','hard') NOT NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  CONSTRAINT `fk_questions_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `question_concepts` (
  `question_id` CHAR(36) NOT NULL,
  `concept_id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`question_id`, `concept_id`),
  UNIQUE KEY `uq_question_concepts_0` (`question_id`, `concept_version_id`),
  CONSTRAINT `fk_question_concepts_0` FOREIGN KEY (`question_id`) REFERENCES `questions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_concepts_1` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_concepts_2` FOREIGN KEY (`concept_version_id`, `concept_id`) REFERENCES `concept_versions` (`id`, `concept_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `question_evidence` (
  `question_id` CHAR(36) NOT NULL,
  `chunk_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`question_id`, `chunk_id`),
  CONSTRAINT `fk_question_evidence_0` FOREIGN KEY (`question_id`) REFERENCES `questions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_evidence_1` FOREIGN KEY (`chunk_id`) REFERENCES `material_chunks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `question_set_items` (
  `set_id` CHAR(36) NOT NULL,
  `question_id` CHAR(36) NOT NULL,
  `ordinal` SMALLINT NOT NULL,
  PRIMARY KEY (`set_id`, `question_id`),
  UNIQUE KEY `uq_question_set_items_0` (`set_id`, `ordinal`),
  UNIQUE KEY `uq_question_set_items_1` (`question_id`),
  CONSTRAINT `fk_question_set_items_0` FOREIGN KEY (`set_id`) REFERENCES `question_sets` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_set_items_1` FOREIGN KEY (`question_id`) REFERENCES `questions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_question_set_items_0` CHECK (ordinal BETWEEN 1 AND 20)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `question_attempts` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `question_id` CHAR(36) NOT NULL,
  `question_set_id` CHAR(36) NOT NULL,
  `answer` JSON NOT NULL,
  `status` ENUM('grading','graded','failed') NOT NULL,
  `is_correct` BOOLEAN NULL,
  `grading_feedback` TEXT NULL,
  `correct_answer` JSON NULL,
  `explanation` TEXT NULL,
  `job_id` CHAR(36) NOT NULL,
  `answered_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_question_attempts_0` (`job_id`),
  CONSTRAINT `fk_question_attempts_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_attempts_1` FOREIGN KEY (`question_set_id`, `question_id`) REFERENCES `question_set_items` (`set_id`, `question_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_question_attempts_2` FOREIGN KEY (`job_id`) REFERENCES `ai_jobs` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_question_attempts_0` CHECK ((status = 'graded' AND is_correct IS NOT NULL AND grading_feedback IS NOT NULL AND correct_answer IS NOT NULL AND explanation IS NOT NULL) OR (status <> 'graded' AND is_correct IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `teachback_sessions` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  `task_id` CHAR(36) NULL,
  `attempt_id` CHAR(36) NULL,
  `previous_session_id` CHAR(36) NULL,
  `explanation_language` ENUM('ko','en') NOT NULL,
  `student_persona` ENUM('curious_beginner','confused_friend','exam_candidate') NOT NULL,
  `status` ENUM('active','report_pending','completed','abandoned') NOT NULL,
  `question_count` TINYINT NOT NULL,
  `started_at` DATETIME(3) NOT NULL,
  `completed_at` DATETIME(3) NULL,
  `latest_job_id` CHAR(36) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_teachback_sessions_0` (`id`, `concept_version_id`),
  CONSTRAINT `fk_teachback_sessions_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_teachback_sessions_1` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_teachback_sessions_2` FOREIGN KEY (`task_id`) REFERENCES `plan_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_teachback_sessions_3` FOREIGN KEY (`attempt_id`) REFERENCES `question_attempts` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_teachback_sessions_4` FOREIGN KEY (`previous_session_id`) REFERENCES `teachback_sessions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_teachback_sessions_5` FOREIGN KEY (`latest_job_id`) REFERENCES `ai_jobs` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_teachback_sessions_0` CHECK (question_count BETWEEN 0 AND 3),
  CONSTRAINT `ck_teachback_sessions_1` CHECK ((status = 'completed' AND completed_at IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `session_messages` (
  `id` CHAR(36) NOT NULL,
  `session_id` CHAR(36) NOT NULL,
  `sequence_no` INT NOT NULL,
  `role` ENUM('user','assistant') NOT NULL,
  `input_mode` ENUM('text','voice_transcript') NOT NULL,
  `content` LONGTEXT NOT NULL,
  `job_id` CHAR(36) NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_session_messages_0` (`session_id`, `sequence_no`),
  UNIQUE KEY `uq_session_messages_1` (`job_id`, `role`),
  CONSTRAINT `fk_session_messages_0` FOREIGN KEY (`session_id`) REFERENCES `teachback_sessions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_session_messages_1` FOREIGN KEY (`job_id`) REFERENCES `ai_jobs` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_session_messages_0` CHECK (sequence_no >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `session_reports` (
  `id` CHAR(36) NOT NULL,
  `session_id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  `coverage` DECIMAL(5,4) NULL,
  `coverage_formula` VARCHAR(32) NOT NULL DEFAULT 'weighted-v1',
  `assessable_count` SMALLINT NOT NULL,
  `weighted_score` DECIMAL(6,2) NOT NULL,
  `next_action` TEXT NOT NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_session_reports_0` (`session_id`),
  UNIQUE KEY `uq_session_reports_1` (`id`, `concept_version_id`),
  CONSTRAINT `fk_session_reports_0` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_session_reports_1` FOREIGN KEY (`session_id`, `concept_version_id`) REFERENCES `teachback_sessions` (`id`, `concept_version_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_session_reports_0` CHECK (assessable_count >= 0),
  CONSTRAINT `ck_session_reports_1` CHECK (weighted_score >= 0 AND weighted_score <= assessable_count),
  CONSTRAINT `ck_session_reports_2` CHECK (coverage_formula = 'weighted-v1'),
  CONSTRAINT `ck_session_reports_3` CHECK ((assessable_count = 0 AND coverage IS NULL AND weighted_score = 0) OR (assessable_count > 0 AND coverage IS NOT NULL AND coverage = ROUND(weighted_score / assessable_count, 4)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `report_items` (
  `id` CHAR(36) NOT NULL,
  `report_id` CHAR(36) NOT NULL,
  `concept_version_id` CHAR(36) NOT NULL,
  `checklist_item_id` CHAR(36) NOT NULL,
  `status` ENUM('explained','partial','omission','error','unverified') NOT NULL,
  `description` TEXT NOT NULL,
  `confidence` ENUM('low','medium','high') NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_report_items_0` (`report_id`, `checklist_item_id`),
  UNIQUE KEY `uq_report_items_1` (`id`, `report_id`),
  CONSTRAINT `fk_report_items_0` FOREIGN KEY (`concept_version_id`) REFERENCES `concept_versions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_report_items_1` FOREIGN KEY (`report_id`, `concept_version_id`) REFERENCES `session_reports` (`id`, `concept_version_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_report_items_2` FOREIGN KEY (`checklist_item_id`, `concept_version_id`) REFERENCES `concept_checklist_items` (`id`, `concept_version_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `report_evidence` (
  `report_item_id` CHAR(36) NOT NULL,
  `chunk_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`report_item_id`, `chunk_id`),
  CONSTRAINT `fk_report_evidence_0` FOREIGN KEY (`report_item_id`) REFERENCES `report_items` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_report_evidence_1` FOREIGN KEY (`chunk_id`) REFERENCES `material_chunks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `improvement_actions` (
  `id` CHAR(36) NOT NULL,
  `report_id` CHAR(36) NOT NULL,
  `concept_id` CHAR(36) NOT NULL,
  `status` ENUM('pending','attempted','completed') NOT NULL,
  `followup_session_id` CHAR(36) NULL,
  `updated_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_improvement_actions_0` (`report_id`),
  CONSTRAINT `fk_improvement_actions_0` FOREIGN KEY (`report_id`) REFERENCES `session_reports` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_improvement_actions_1` FOREIGN KEY (`concept_id`) REFERENCES `concepts` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_improvement_actions_2` FOREIGN KEY (`followup_session_id`) REFERENCES `teachback_sessions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `feedback_reports` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `session_id` CHAR(36) NOT NULL,
  `report_id` CHAR(36) NOT NULL,
  `report_item_id` CHAR(36) NULL,
  `reason` ENUM('incorrect','unsupported','unclear','other') NOT NULL,
  `comment` VARCHAR(2000) NULL,
  `status` ENUM('received','reviewing','resolved','rejected') NOT NULL,
  `created_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  CONSTRAINT `fk_feedback_reports_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_feedback_reports_1` FOREIGN KEY (`session_id`) REFERENCES `teachback_sessions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_feedback_reports_2` FOREIGN KEY (`report_id`) REFERENCES `session_reports` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_feedback_reports_3` FOREIGN KEY (`report_item_id`, `report_id`) REFERENCES `report_items` (`id`, `report_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `publications` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `question_id` CHAR(36) NOT NULL,
  `status` ENUM('active','withdrawn') NOT NULL,
  `question_type` ENUM('multiple_choice','short_answer') NOT NULL,
  `prompt` LONGTEXT NOT NULL,
  `options` JSON NOT NULL,
  `answer_key` JSON NOT NULL,
  `accepted_answers` JSON NOT NULL,
  `explanation` TEXT NOT NULL,
  `difficulty` ENUM('easy','medium','hard') NOT NULL,
  `consented_at` DATETIME(3) NOT NULL,
  `published_at` DATETIME(3) NOT NULL,
  `withdrawn_at` DATETIME(3) NULL,
  `active_question_id` CHAR(36) GENERATED ALWAYS AS (CASE WHEN status = 'active' THEN question_id ELSE NULL END) STORED,
  `keywords` JSON NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_publications_0` (`active_question_id`),
  CONSTRAINT `fk_publications_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_publications_1` FOREIGN KEY (`question_id`) REFERENCES `questions` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `public_attempts` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `publication_id` CHAR(36) NOT NULL,
  `answer` JSON NOT NULL,
  `is_correct` BOOLEAN NOT NULL,
  `correct_answer` JSON NOT NULL,
  `explanation` TEXT NOT NULL,
  `question_snapshot` JSON NOT NULL,
  `answered_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  CONSTRAINT `fk_public_attempts_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_public_attempts_1` FOREIGN KEY (`publication_id`) REFERENCES `publications` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `score_claims` (
  `id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `public_attempt_id` CHAR(36) NOT NULL,
  `exam_type` ENUM('toeic','toefl','entrance_exam','other') NOT NULL,
  `score` DECIMAL(10,2) NOT NULL,
  `max_score` DECIMAL(10,2) NOT NULL,
  `exam_date` DATE NOT NULL,
  `status` ENUM('upload_pending','submitted','verified','rejected','withdrawn') NOT NULL,
  `proof_storage_key` VARCHAR(512) NOT NULL,
  `mime_type` VARCHAR(100) NOT NULL,
  `size_bytes` BIGINT NOT NULL,
  `reviewer_id` CHAR(36) NULL,
  `reason` VARCHAR(1000) NULL,
  `decided_at` DATETIME(3) NULL,
  `proof_deleted_at` DATETIME(3) NULL,
  `created_at` DATETIME(3) NOT NULL,
  `active_attempt_id` CHAR(36) GENERATED ALWAYS AS (CASE WHEN status IN ('upload_pending','submitted','verified') THEN public_attempt_id ELSE NULL END) STORED,
  `public_score_consented_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_score_claims_0` (`active_attempt_id`, `exam_date`),
  CONSTRAINT `fk_score_claims_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_score_claims_1` FOREIGN KEY (`public_attempt_id`) REFERENCES `public_attempts` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_score_claims_2` FOREIGN KEY (`reviewer_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_score_claims_0` CHECK (score >= 0 AND max_score > 0 AND score <= max_score),
  CONSTRAINT `ck_score_claims_1` CHECK (size_bytes BETWEEN 1 AND 10485760),
  CONSTRAINT `ck_score_claims_2` CHECK (reviewer_id IS NULL OR reviewer_id <> user_id),
  CONSTRAINT `ck_score_claims_3` CHECK (exam_type <> 'toeic' OR max_score = 990),
  CONSTRAINT `ck_score_claims_4` CHECK (exam_type <> 'toefl' OR max_score = 120)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `idempotency_requests` (
  `user_id` CHAR(36) NOT NULL,
  `operation_id` VARCHAR(120) NOT NULL,
  `idempotency_key` VARCHAR(128) NOT NULL,
  `request_hash` CHAR(64) NOT NULL,
  `status` ENUM('in_progress','completed') NOT NULL,
  `http_status` SMALLINT NULL,
  `response_body` JSON NULL,
  `created_at` DATETIME(3) NOT NULL,
  `expires_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`user_id`, `operation_id`, `idempotency_key`),
  CONSTRAINT `fk_idempotency_requests_0` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `storage_deletion_outbox` (
  `id` CHAR(36) NOT NULL,
  `storage_key` VARCHAR(512) NOT NULL,
  `delete_after` DATETIME(3) NOT NULL,
  `deleted_at` DATETIME(3) NULL,
  `attempt_count` INT NOT NULL,
  PRIMARY KEY (`id`),
  CONSTRAINT `ck_storage_deletion_outbox_0` CHECK (attempt_count >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Application transactional invariants (not falsely claimed as enforced by FK alone):
-- All user-owned joins must have the authenticated owner; cross-owner ID =>404.
-- Concept evidence chunks share the concept material. Question evidence shares set unit material.
-- Set versions belong to its unit; generated question versions are a nonempty subset of set versions.
-- Task version set matches set/session target; task is active revision and correct type.
-- Confirmed version/checklist/evidence are immutable; current=MAX(version_no), CAS base_version_id.
-- A confirmed version needs >=1 checklist and evidence; summary nonempty.
-- Learning explanations are separate immutable successful resources, not concept summary updates
-- or Teach-back sessions. Generating/reading them never marks a plan task complete or changes coverage.
-- Create accepts an owned confirmed concept_version_id (historical confirmed versions allowed),
-- with ready, nonarchived material. Resolve user/concept/material IDs from that pinned version;
-- verify all owner joins. Draft or empty evidence =>422; absent/cross-owner version =>404.
-- Learning explanation POST atomically creates resource+queued job with kind/resource_type
-- learning_explanation; normalized fingerprint includes owner, concept_version_id, language,
-- model_id. One active equal fingerprint; same idempotency key replays the accepted response.
-- Different idempotency keys with an active equal fingerprint conflict409; no extra provider call.
-- The immutable job request_payload pins version/language/model, never API keys; inputs use the
-- pinned version's immutable summary/checklist/evidence, not whichever version later becomes current.
-- API LearningExplanation.model_id is required read-only metadata derived from the current
-- latest_job_id -> ai_jobs.model_id, not a new explanation column. Verify job owner, kind,
-- resource_type and resource_id match this explanation; retry/restart preserve the same model.
-- Return model_id on detail/list reads so re-entry can restore retry/restart inputs safely.
-- ready requires >=1 unique evidence row, valid nonempty title/markdown, and generated_at. Validate
-- every excerpt against its source chunk; unknown/unrelated citations or unsupported output fail
-- INVALID_OUTPUT (retryable=false). Content+evidence+job succeeded commit atomically under fencing.
-- Nonready explanations have NULL content_title/content_markdown/generated_at and zero evidence rows;
-- partial provider output must not leak. API content=null until ready (no evidence key outside
-- content); ready API content={title,explanation_markdown,evidence:[SourceEvidence,...]}.
-- queued/running jobs map to explanation.generating, failed to failed, outcome_unknown to
-- outcome_unknown, succeeded to ready. Keep resource and current job states transactionally aligned.
-- Retry accepts only current failed retryable job with attempt_no<3, same model, fresh credential;
-- it reuses job/resource IDs and atomically resets resource to generating. No automatic paid retries.
-- Restart accepts current failed/outcome_unknown job only, fresh key and explicit possible duplicate
-- cost consent; creates successor job on the SAME explanation and resets to generating under row lock.
-- Ready content is never overwritten; generating a new explanation after success creates a new ID.
-- Explanation GET/list verify owner; archived materials remain readable, no public evidence access.
-- List filter is exact concept_version_id, never a mixture of concept versions; created_at DESC,id DESC.
-- Explanations/evidence block hard deletion of their material/version/chunks, even after terminal
-- failure; archive retains readable history. No new delete route or silent cascade is introduced.
-- TodaySummary.days_remaining is derived, not stored: max(0, exam_date - Asia/Seoul response date),
-- or NULL only when no goal scope exists. Exam day and past exam dates return0, not negative values.
-- PlanTask.display_order=plan_tasks.ordinal (1-based); response tasks sort by ordinal ASC,id ASC.
-- PlanTask.concepts is a read projection of plan_task_concepts joined to pinned concept_versions,
-- concepts, material_units and materials; never join the newest version for concept_name.
-- concepts order is concept_version_id ASC; concept_ids/concept_version_ids use exactly that order.
-- PlanTask.title is deterministic presentation derived from pinned concept names and task_type;
-- no title, days_remaining, daily_minutes or estimated-time column is added.
-- Set item ordinals contiguous 1..requested_count; insert complete batch or none.
-- Report item set equals ALL checklist IDs of pinned version, no extras/duplicates; compute coverage.
-- report/error evidence required; report evidence same material as pinned concept version.
-- feedback_reports.report_id belongs to session_id; user owns session/report/item.
-- Active job locks: material analysis; question generation fingerprint; question grading;
-- learning explanation fingerprint/resource (including retries and restarts);
-- all message/report jobs share a session lock (even across kinds). Fingerprint SHA256 canonical
-- (owner, scope, resource/selection), not secrets. Lease fencing prevents double result commit.
-- Job resource FK is polymorphic and validated in service transaction (not DB FK).
-- Unknown provider outcome cannot be retried automatically or via retry route.
-- Terminal restart requires current latest_job pointer, valid failed resource state, no successor,
-- resource row lock, fresh key and explicit possible-duplicate-cost consent. New job replaces pointer.
-- A completed/abandoned session cannot restart an old failed message job (STALE_JOB).
-- Attempt result+job success+task derived progress committed once. Same question counts once/task.
-- Sessions previous/attempt/action links: same owner+concept, preceding time; same-version trend only.
-- No anonymous source access. Publication is an explicit safe snapshot; no private evidence IDs/URLs.
-- Public attempts retain snapshot after withdrawal; proof access emits existing structured audit log.
-- Score claim owner owns public_attempt, different from publisher, exam_date>=attempt local date.
-- score claimed exam_date<=today, verified only after reviewer proof match, reason required.
-- PublicQuestion.verified_score is NULL unless an eligible verified score_claim exists for that
-- publication via public_attempts.publication_id. It describes a learner's verified score, not the
-- publisher's score or proof that this question caused improvement. Public consent must be present.
-- Select one eligible claim by decided_at DESC,id DESC; require status='verified', decided_at and
-- public_score_consented_at non-NULL. Pending/rejected/withdrawn claims never produce a badge.
-- Public DTO exposes ONLY exam_type,score,max_score,exam_date,status='verified'; never claim/user/
-- reviewer/attempt IDs, names, reason, proof key/URL, or evidence. Score stays numeric, no ratios.
-- Withdrawal removes the claim from selection immediately (recompute previous eligible claim or
-- NULL); invalidate cached public projections in the same commit boundary. Proof expiry/deletion
-- alone is not withdrawal and does not erase the historic verification decision.
-- POST publications requires consent_public=true; false is422 and creates no row. consented_at
-- is the server UTC acceptance timestamp; a successful request example must use true.
-- Proof deletion outbox: unsubmitted24h, decided30days; submitted90days expires rejected first.
-- Material DELETE rejected if plan/questions/session/learning_explanation/publication/activejob refs exist; else explicit
-- child deletes + storage outbox, never cascading historical data. Archive remains readable.
-- Idempotency request expiry24h; key comparison case-sensitive; no credentials in JSON payloads.
-- Default result ordering/pagination: created_at DESC,id DESC; messages sequence_no ASC.
-- Add indexes matching query plans after measurement; index tuning not claimed completed here.
