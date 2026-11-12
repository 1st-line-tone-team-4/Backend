-- Canonical A schema integration after 001..004 (fresh databases start at 000).
-- Nonrepeatable migration. Stop all writers; back up before applying.
-- Required input: canonical users and an EMPTY, ID-only CHAR(36) plan_tasks stub.
-- A nonempty stub has no authoritative goal/revision/pins and requires a separately
-- reviewed data backfill. These preflights fail before persistent schema changes.
-- Do not bypass them, delete history, or fabricate task provenance.

CREATE TEMPORARY TABLE ab_005_preflight (
    guard_id BIGINT NOT NULL PRIMARY KEY
) ENGINE=InnoDB;
INSERT INTO ab_005_preflight VALUES (0);
INSERT INTO ab_005_preflight SELECT 0 FROM plan_tasks LIMIT 1;
INSERT INTO ab_005_preflight
SELECT CASE WHEN COUNT(*) = 1 AND SUM(column_name = 'id' AND column_type = 'char(36)'
    AND is_nullable = 'NO' AND character_set_name = 'utf8mb4'
    AND collation_name = 'utf8mb4_bin') = 1 THEN 1 ELSE 0 END
FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'plan_tasks';
INSERT INTO ab_005_preflight
SELECT CASE WHEN COUNT(*) = 7 AND SUM(
    (column_name = 'id' AND column_type = 'char(36)' AND is_nullable = 'NO'
        AND character_set_name = 'utf8mb4' AND collation_name = 'utf8mb4_bin') OR
    (column_name = 'auth_issuer' AND column_type = 'varchar(255)' AND is_nullable = 'NO') OR
    (column_name = 'auth_subject' AND column_type = 'varchar(255)' AND is_nullable = 'NO') OR
    (column_name = 'email' AND column_type = 'varchar(320)' AND is_nullable = 'YES') OR
    (column_name = 'display_name' AND column_type = 'varchar(120)' AND is_nullable = 'YES') OR
    (column_name = 'timezone' AND column_type = 'varchar(64)' AND is_nullable = 'NO') OR
    (column_name = 'created_at' AND column_type = 'datetime(3)' AND is_nullable = 'NO')
) = 7 THEN 2 ELSE 0 END
FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'users';
INSERT INTO ab_005_preflight
SELECT 0 FROM information_schema.tables
WHERE table_schema = DATABASE() AND table_name IN ('exam_goals','feedback_reports','improvement_actions','plan_task_concepts','public_attempts','publications','question_attempts','report_evidence','report_items','score_claims','session_messages','session_reports','study_plan_materials','study_plans','teachback_sessions') LIMIT 1;
DROP TEMPORARY TABLE ab_005_preflight;

ALTER TABLE users ALTER COLUMN timezone SET DEFAULT 'Asia/Seoul';


CREATE TABLE exam_goals (
	id CHAR(36) NOT NULL,
	user_id CHAR(36) NOT NULL,
	title VARCHAR(120) NOT NULL,
	exam_type VARCHAR(32) NOT NULL,
	exam_date DATE NOT NULL,
	status VARCHAR(32) NOT NULL,
	created_at DATETIME(3) NOT NULL,
	updated_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_exam_goals_exam_type CHECK (exam_type IN ('school_exam','toeic','toefl','entrance_exam','other')),
	CONSTRAINT ck_exam_goals_status CHECK (status IN ('active','paused','completed')),
	CONSTRAINT fk_exam_goals_0 FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE study_plans (
	id CHAR(36) NOT NULL,
	user_id CHAR(36) NOT NULL,
	exam_goal_id CHAR(36) NOT NULL,
	revision INTEGER NOT NULL,
	status VARCHAR(32) NOT NULL,
	generated_at DATETIME(3) NOT NULL,
	active_goal_id CHAR(36) GENERATED ALWAYS AS (CASE WHEN status = 'active' THEN exam_goal_id ELSE NULL END) STORED,
	PRIMARY KEY (id),
	CONSTRAINT ck_study_plans_status CHECK (status IN ('active','superseded')),
	CONSTRAINT uq_study_plans_0 UNIQUE (exam_goal_id, revision),
	CONSTRAINT uq_study_plans_1 UNIQUE (active_goal_id),
	CONSTRAINT fk_study_plans_0 FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_study_plans_1 FOREIGN KEY(exam_goal_id) REFERENCES exam_goals (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ck_study_plans_0 CHECK (revision >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

ALTER TABLE plan_tasks
    ADD COLUMN study_plan_id CHAR(36) NOT NULL,
    ADD COLUMN ordinal INTEGER NOT NULL,
    ADD COLUMN due_date DATE NOT NULL,
    ADD COLUMN task_type VARCHAR(32) NOT NULL,
    ADD COLUMN target_count SMALLINT NOT NULL,
    ADD COLUMN status VARCHAR(32) NOT NULL,
    ADD COLUMN completed_at DATETIME(3),
    ADD COLUMN created_at DATETIME(3) NOT NULL,
    ADD CONSTRAINT ck_plan_tasks_0 CHECK (ordinal >= 1),
    ADD CONSTRAINT ck_plan_tasks_1 CHECK (target_count BETWEEN 1 AND 20),
    ADD CONSTRAINT ck_plan_tasks_2 CHECK ((status = 'completed' AND completed_at IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL)),
    ADD CONSTRAINT ck_plan_tasks_status CHECK (status IN ('pending','in_progress','completed','skipped')),
    ADD CONSTRAINT ck_plan_tasks_task_type CHECK (task_type IN ('solve','teach_back','review')),
    ADD CONSTRAINT fk_plan_tasks_0 FOREIGN KEY(study_plan_id) REFERENCES study_plans (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    ADD CONSTRAINT uq_plan_tasks_0 UNIQUE (study_plan_id, ordinal);

CREATE TABLE publications (
	id CHAR(36) NOT NULL,
	user_id CHAR(36) NOT NULL,
	question_id CHAR(36) NOT NULL,
	status VARCHAR(32) NOT NULL,
	question_type VARCHAR(32) NOT NULL,
	prompt LONGTEXT NOT NULL,
	options JSON NOT NULL,
	answer_key JSON NOT NULL,
	accepted_answers JSON NOT NULL,
	explanation TEXT NOT NULL,
	difficulty VARCHAR(32) NOT NULL,
	consented_at DATETIME(3) NOT NULL,
	published_at DATETIME(3) NOT NULL,
	withdrawn_at DATETIME(3),
	active_question_id CHAR(36) GENERATED ALWAYS AS (CASE WHEN status = 'active' THEN question_id ELSE NULL END) STORED,
	keywords JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_publications_status CHECK (status IN ('active','withdrawn')),
	CONSTRAINT ck_publications_question_type CHECK (question_type IN ('multiple_choice','short_answer')),
	CONSTRAINT ck_publications_difficulty CHECK (difficulty IN ('easy','medium','hard')),
	CONSTRAINT uq_publications_0 UNIQUE (active_question_id),
	CONSTRAINT fk_publications_0 FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_publications_1 FOREIGN KEY(question_id) REFERENCES questions (id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE public_attempts (
	id CHAR(36) NOT NULL,
	user_id CHAR(36) NOT NULL,
	publication_id CHAR(36) NOT NULL,
	answer JSON NOT NULL,
	is_correct BOOL NOT NULL,
	correct_answer JSON NOT NULL,
	explanation TEXT NOT NULL,
	question_snapshot JSON NOT NULL,
	answered_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_public_attempts_0 FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_public_attempts_1 FOREIGN KEY(publication_id) REFERENCES publications (id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE study_plan_materials (
	study_plan_id CHAR(36) NOT NULL,
	material_id CHAR(36) NOT NULL,
	ordinal INTEGER NOT NULL,
	PRIMARY KEY (study_plan_id, material_id),
	CONSTRAINT uq_study_plan_materials_0 UNIQUE (study_plan_id, ordinal),
	CONSTRAINT fk_study_plan_materials_0 FOREIGN KEY(study_plan_id) REFERENCES study_plans (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_study_plan_materials_1 FOREIGN KEY(material_id) REFERENCES materials (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ck_study_plan_materials_0 CHECK (ordinal >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE score_claims (
	id CHAR(36) NOT NULL,
	user_id CHAR(36) NOT NULL,
	public_attempt_id CHAR(36) NOT NULL,
	exam_type VARCHAR(32) NOT NULL,
	score NUMERIC(10, 2) NOT NULL,
	max_score NUMERIC(10, 2) NOT NULL,
	exam_date DATE NOT NULL,
	status VARCHAR(32) NOT NULL,
	proof_storage_key VARCHAR(512) NOT NULL,
	mime_type VARCHAR(100) NOT NULL,
	size_bytes BIGINT NOT NULL,
	reviewer_id CHAR(36),
	reason VARCHAR(1000),
	decided_at DATETIME(3),
	proof_deleted_at DATETIME(3),
	created_at DATETIME(3) NOT NULL,
	active_attempt_id CHAR(36) GENERATED ALWAYS AS (CASE WHEN status IN ('upload_pending','submitted','verified') THEN public_attempt_id ELSE NULL END) STORED,
	public_score_consented_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_score_claims_exam_type CHECK (exam_type IN ('toeic','toefl','entrance_exam','other')),
	CONSTRAINT ck_score_claims_status CHECK (status IN ('upload_pending','submitted','verified','rejected','withdrawn')),
	CONSTRAINT uq_score_claims_0 UNIQUE (active_attempt_id, exam_date),
	CONSTRAINT fk_score_claims_0 FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_score_claims_1 FOREIGN KEY(public_attempt_id) REFERENCES public_attempts (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_score_claims_2 FOREIGN KEY(reviewer_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ck_score_claims_0 CHECK (score >= 0 AND max_score > 0 AND score <= max_score),
	CONSTRAINT ck_score_claims_1 CHECK (size_bytes BETWEEN 1 AND 10485760),
	CONSTRAINT ck_score_claims_2 CHECK (reviewer_id IS NULL OR reviewer_id <> user_id),
	CONSTRAINT ck_score_claims_3 CHECK (exam_type <> 'toeic' OR max_score = 990),
	CONSTRAINT ck_score_claims_4 CHECK (exam_type <> 'toefl' OR max_score = 120)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE plan_task_concepts (
	task_id CHAR(36) NOT NULL,
	concept_id CHAR(36) NOT NULL,
	concept_version_id CHAR(36) NOT NULL,
	PRIMARY KEY (task_id, concept_id),
	CONSTRAINT uq_plan_task_concepts_0 UNIQUE (task_id, concept_version_id),
	CONSTRAINT fk_plan_task_concepts_0 FOREIGN KEY(task_id) REFERENCES plan_tasks (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_plan_task_concepts_1 FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_plan_task_concepts_2 FOREIGN KEY(concept_version_id, concept_id) REFERENCES concept_versions (id, concept_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE question_attempts (
	id CHAR(36) NOT NULL,
	user_id CHAR(36) NOT NULL,
	question_id CHAR(36) NOT NULL,
	question_set_id CHAR(36) NOT NULL,
	answer JSON NOT NULL,
	status VARCHAR(32) NOT NULL,
	is_correct BOOL,
	grading_feedback TEXT,
	correct_answer JSON,
	explanation TEXT,
	job_id CHAR(36) NOT NULL,
	answered_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_question_attempts_status CHECK (status IN ('grading','graded','failed')),
	CONSTRAINT uq_question_attempts_0 UNIQUE (job_id),
	CONSTRAINT fk_question_attempts_0 FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_question_attempts_1 FOREIGN KEY(question_set_id, question_id) REFERENCES question_set_items (set_id, question_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_question_attempts_2 FOREIGN KEY(job_id) REFERENCES ai_jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ck_question_attempts_0 CHECK ((status = 'graded' AND is_correct IS NOT NULL AND grading_feedback IS NOT NULL AND correct_answer IS NOT NULL AND explanation IS NOT NULL) OR (status <> 'graded' AND is_correct IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE teachback_sessions (
	id CHAR(36) NOT NULL,
	user_id CHAR(36) NOT NULL,
	concept_version_id CHAR(36) NOT NULL,
	task_id CHAR(36),
	attempt_id CHAR(36),
	previous_session_id CHAR(36),
	explanation_language VARCHAR(32) NOT NULL,
	student_persona VARCHAR(32) NOT NULL,
	status VARCHAR(32) NOT NULL,
	question_count SMALLINT NOT NULL,
	started_at DATETIME(3) NOT NULL,
	completed_at DATETIME(3),
	latest_job_id CHAR(36),
	PRIMARY KEY (id),
	CONSTRAINT ck_teachback_sessions_explanation_language CHECK (explanation_language IN ('ko','en')),
	CONSTRAINT ck_teachback_sessions_student_persona CHECK (student_persona IN ('curious_beginner','confused_friend','exam_candidate')),
	CONSTRAINT ck_teachback_sessions_status CHECK (status IN ('active','report_pending','completed','abandoned')),
	CONSTRAINT uq_teachback_sessions_0 UNIQUE (id, concept_version_id),
	CONSTRAINT fk_teachback_sessions_0 FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_teachback_sessions_1 FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_teachback_sessions_2 FOREIGN KEY(task_id) REFERENCES plan_tasks (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_teachback_sessions_3 FOREIGN KEY(attempt_id) REFERENCES question_attempts (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_teachback_sessions_4 FOREIGN KEY(previous_session_id) REFERENCES teachback_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_teachback_sessions_5 FOREIGN KEY(latest_job_id) REFERENCES ai_jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ck_teachback_sessions_0 CHECK (question_count BETWEEN 0 AND 3),
	CONSTRAINT ck_teachback_sessions_1 CHECK ((status = 'completed' AND completed_at IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE session_messages (
	id CHAR(36) NOT NULL,
	session_id CHAR(36) NOT NULL,
	sequence_no INTEGER NOT NULL,
	`role` VARCHAR(32) NOT NULL,
	input_mode VARCHAR(32) NOT NULL,
	content LONGTEXT NOT NULL,
	job_id CHAR(36),
	created_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_session_messages_role CHECK (role IN ('user','assistant')),
	CONSTRAINT ck_session_messages_input_mode CHECK (input_mode IN ('text','voice_transcript')),
	CONSTRAINT uq_session_messages_0 UNIQUE (session_id, sequence_no),
	CONSTRAINT uq_session_messages_1 UNIQUE (job_id, `role`),
	CONSTRAINT fk_session_messages_0 FOREIGN KEY(session_id) REFERENCES teachback_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_session_messages_1 FOREIGN KEY(job_id) REFERENCES ai_jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ck_session_messages_0 CHECK (sequence_no >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE session_reports (
	id CHAR(36) NOT NULL,
	session_id CHAR(36) NOT NULL,
	concept_version_id CHAR(36) NOT NULL,
	coverage NUMERIC(5, 4),
	coverage_formula VARCHAR(32) NOT NULL DEFAULT 'weighted-v1',
	assessable_count SMALLINT NOT NULL,
	weighted_score NUMERIC(6, 2) NOT NULL,
	next_action TEXT NOT NULL,
	created_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_session_reports_0 UNIQUE (session_id),
	CONSTRAINT uq_session_reports_1 UNIQUE (id, concept_version_id),
	CONSTRAINT fk_session_reports_0 FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_session_reports_1 FOREIGN KEY(session_id, concept_version_id) REFERENCES teachback_sessions (id, concept_version_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ck_session_reports_0 CHECK (assessable_count >= 0),
	CONSTRAINT ck_session_reports_1 CHECK (weighted_score >= 0 AND weighted_score <= assessable_count),
	CONSTRAINT ck_session_reports_2 CHECK (coverage_formula = 'weighted-v1'),
	CONSTRAINT ck_session_reports_3 CHECK ((assessable_count = 0 AND coverage IS NULL AND weighted_score = 0) OR (assessable_count > 0 AND coverage IS NOT NULL AND coverage = ROUND(weighted_score * 1.0 / assessable_count, 4)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE improvement_actions (
	id CHAR(36) NOT NULL,
	report_id CHAR(36) NOT NULL,
	concept_id CHAR(36) NOT NULL,
	status VARCHAR(32) NOT NULL,
	followup_session_id CHAR(36),
	updated_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_improvement_actions_status CHECK (status IN ('pending','attempted','completed')),
	CONSTRAINT uq_improvement_actions_0 UNIQUE (report_id),
	CONSTRAINT fk_improvement_actions_0 FOREIGN KEY(report_id) REFERENCES session_reports (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_improvement_actions_1 FOREIGN KEY(concept_id) REFERENCES concepts (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_improvement_actions_2 FOREIGN KEY(followup_session_id) REFERENCES teachback_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE report_items (
	id CHAR(36) NOT NULL,
	report_id CHAR(36) NOT NULL,
	concept_version_id CHAR(36) NOT NULL,
	checklist_item_id CHAR(36) NOT NULL,
	status VARCHAR(32) NOT NULL,
	description TEXT NOT NULL,
	confidence VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_report_items_status CHECK (status IN ('explained','partial','omission','error','unverified')),
	CONSTRAINT ck_report_items_confidence CHECK (confidence IN ('low','medium','high')),
	CONSTRAINT uq_report_items_0 UNIQUE (report_id, checklist_item_id),
	CONSTRAINT uq_report_items_1 UNIQUE (id, report_id),
	CONSTRAINT fk_report_items_0 FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_report_items_1 FOREIGN KEY(report_id, concept_version_id) REFERENCES session_reports (id, concept_version_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_report_items_2 FOREIGN KEY(checklist_item_id, concept_version_id) REFERENCES concept_checklist_items (id, concept_version_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE feedback_reports (
	id CHAR(36) NOT NULL,
	user_id CHAR(36) NOT NULL,
	session_id CHAR(36) NOT NULL,
	report_id CHAR(36) NOT NULL,
	report_item_id CHAR(36),
	reason VARCHAR(32) NOT NULL,
	comment VARCHAR(2000),
	status VARCHAR(32) NOT NULL,
	created_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_feedback_reports_reason CHECK (reason IN ('incorrect','unsupported','unclear','other')),
	CONSTRAINT ck_feedback_reports_status CHECK (status IN ('received','reviewing','resolved','rejected')),
	CONSTRAINT fk_feedback_reports_0 FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_feedback_reports_1 FOREIGN KEY(session_id) REFERENCES teachback_sessions (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_feedback_reports_2 FOREIGN KEY(report_id) REFERENCES session_reports (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_feedback_reports_3 FOREIGN KEY(report_item_id, report_id) REFERENCES report_items (id, report_id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE report_evidence (
	report_item_id CHAR(36) NOT NULL,
	chunk_id CHAR(36) NOT NULL,
	PRIMARY KEY (report_item_id, chunk_id),
	CONSTRAINT fk_report_evidence_0 FOREIGN KEY(report_item_id) REFERENCES report_items (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT fk_report_evidence_1 FOREIGN KEY(chunk_id) REFERENCES material_chunks (id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
