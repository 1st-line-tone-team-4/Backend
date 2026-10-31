-- New B schema only. Prerequisites: existing compatible users(id) and plan_tasks(id).
-- See README.md before execution. Do not apply to an existing B schema.

CREATE TABLE storage_deletion_outbox (
	id CHAR(36) NOT NULL, 
	storage_key VARCHAR(512) NOT NULL, 
	delete_after DATETIME(3) NOT NULL, 
	deleted_at DATETIME(3), 
	attempt_count INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (attempt_count >= 0)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE ai_jobs (
	id CHAR(36) NOT NULL, 
	user_id CHAR(36) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	resource_type VARCHAR(32) NOT NULL, 
	resource_id CHAR(36) NOT NULL, 
	request_fingerprint VARCHAR(64) NOT NULL, 
	request_payload JSON NOT NULL, 
	model_id VARCHAR(100) NOT NULL, 
	attempt_no SMALLINT NOT NULL, 
	retryable BOOL NOT NULL, 
	error_code VARCHAR(64), 
	result_url VARCHAR(512), 
	fencing_token BIGINT NOT NULL, 
	lease_expires_at DATETIME(3), 
	created_at DATETIME(3) NOT NULL, 
	updated_at DATETIME(3) NOT NULL, 
	active_fingerprint VARCHAR(64) GENERATED ALWAYS AS (CASE WHEN status IN ('queued','running') THEN request_fingerprint ELSE NULL END) STORED, 
	previous_job_id CHAR(36), 
	PRIMARY KEY (id), 
	CHECK (attempt_no BETWEEN 1 AND 3), 
	CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'outcome_unknown')), 
	CHECK (fencing_token >= 0), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(previous_job_id) REFERENCES ai_jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	CHECK (resource_type IN ('material', 'question_set', 'attempt', 'session', 'learning_explanation')), 
	CHECK (status = 'failed' OR retryable = FALSE), 
	CHECK (kind IN ('analysis', 'questions', 'grade', 'message', 'report', 'learning_explanation')), 
	UNIQUE (previous_job_id), 
	UNIQUE (user_id, active_fingerprint)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE idempotency_requests (
	user_id CHAR(36) NOT NULL, 
	operation_id VARCHAR(120) NOT NULL, 
	idempotency_key VARCHAR(128) COLLATE utf8mb4_0900_bin NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	http_status SMALLINT, 
	response_body JSON, 
	created_at DATETIME(3) NOT NULL, 
	expires_at DATETIME(3) NOT NULL, 
	PRIMARY KEY (user_id, operation_id, idempotency_key), 
	CHECK (status IN ('in_progress', 'completed')), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE questions (
	id CHAR(36) NOT NULL, 
	user_id CHAR(36) NOT NULL, 
	question_type VARCHAR(32) NOT NULL, 
	prompt LONGTEXT NOT NULL, 
	options JSON NOT NULL, 
	answer_key JSON NOT NULL, 
	public_explanation TEXT NOT NULL, 
	accepted_answers JSON NOT NULL, 
	difficulty VARCHAR(32) NOT NULL, 
	created_at DATETIME(3) NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (difficulty IN ('easy', 'medium', 'hard')), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	CHECK (question_type IN ('multiple_choice', 'short_answer'))
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE materials (
	id CHAR(36) NOT NULL, 
	user_id CHAR(36) NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	mime_type VARCHAR(100) NOT NULL, 
	size_bytes BIGINT NOT NULL, 
	storage_key VARCHAR(512) NOT NULL, 
	processing_status VARCHAR(32) NOT NULL, 
	page_count INTEGER, 
	archived BOOL NOT NULL, 
	created_at DATETIME(3) NOT NULL, 
	updated_at DATETIME(3) NOT NULL, 
	latest_job_id CHAR(36), 
	PRIMARY KEY (id), 
	CHECK (processing_status IN ('upload_pending', 'queued', 'processing', 'ready', 'failed')), 
	FOREIGN KEY(latest_job_id) REFERENCES ai_jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	CHECK (page_count IS NULL OR page_count >= 1), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (storage_key), 
	CHECK (size_bytes BETWEEN 1 AND 20971520)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE material_chunks (
	id CHAR(36) NOT NULL, 
	material_id CHAR(36) NOT NULL, 
	page_number INTEGER NOT NULL, 
	ordinal INTEGER NOT NULL, 
	content LONGTEXT NOT NULL, 
	embedding_ref VARCHAR(512), 
	created_at DATETIME(3) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id, material_id), 
	FOREIGN KEY(material_id) REFERENCES materials (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (material_id, ordinal), 
	CHECK (page_number >= 1), 
	CHECK (ordinal >= 1)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE material_units (
	id CHAR(36) NOT NULL, 
	material_id CHAR(36) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (ordinal >= 1), 
	FOREIGN KEY(material_id) REFERENCES materials (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (material_id, ordinal), 
	UNIQUE (id, material_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE concepts (
	id CHAR(36) NOT NULL, 
	material_id CHAR(36) NOT NULL, 
	unit_id CHAR(36) NOT NULL, 
	created_at DATETIME(3) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id, material_id), 
	FOREIGN KEY(material_id) REFERENCES materials (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (id, unit_id), 
	FOREIGN KEY(unit_id, material_id) REFERENCES material_units (id, material_id) ON DELETE RESTRICT ON UPDATE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE question_evidence (
	question_id CHAR(36) NOT NULL, 
	chunk_id CHAR(36) NOT NULL, 
	PRIMARY KEY (question_id, chunk_id), 
	FOREIGN KEY(chunk_id) REFERENCES material_chunks (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(question_id) REFERENCES questions (id) ON DELETE RESTRICT ON UPDATE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE question_sets (
	id CHAR(36) NOT NULL, 
	user_id CHAR(36) NOT NULL, 
	unit_id CHAR(36) NOT NULL, 
	task_id CHAR(36), 
	difficulty VARCHAR(32) NOT NULL, 
	requested_count SMALLINT NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	job_id CHAR(36) NOT NULL, 
	created_at DATETIME(3) NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (difficulty IN ('easy', 'medium', 'hard')), 
	FOREIGN KEY(task_id) REFERENCES plan_tasks (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(unit_id) REFERENCES material_units (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(job_id) REFERENCES ai_jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	CHECK (requested_count BETWEEN 1 AND 20), 
	CHECK (status IN ('generating', 'ready', 'failed')), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (job_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE concept_versions (
	id CHAR(36) NOT NULL, 
	concept_id CHAR(36) NOT NULL, 
	version_no INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	summary TEXT, 
	is_confirmed BOOL NOT NULL, 
	confirmed_at DATETIME(3), 
	created_at DATETIME(3) NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (version_no >= 1), 
	CHECK ((is_confirmed = FALSE AND confirmed_at IS NULL) OR (is_confirmed = TRUE AND confirmed_at IS NOT NULL AND summary IS NOT NULL AND char_length(summary) > 0)), 
	UNIQUE (id, concept_id), 
	FOREIGN KEY(concept_id) REFERENCES concepts (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (concept_id, version_no)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE question_set_items (
	set_id CHAR(36) NOT NULL, 
	question_id CHAR(36) NOT NULL, 
	ordinal SMALLINT NOT NULL, 
	PRIMARY KEY (set_id, question_id), 
	UNIQUE (set_id, ordinal), 
	FOREIGN KEY(question_id) REFERENCES questions (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(set_id) REFERENCES question_sets (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (question_id), 
	CHECK (ordinal BETWEEN 1 AND 20)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE concept_checklist_items (
	id CHAR(36) NOT NULL, 
	concept_version_id CHAR(36) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	criterion TEXT NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (ordinal BETWEEN 1 AND 50), 
	FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (concept_version_id, ordinal), 
	UNIQUE (id, concept_version_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE concept_version_evidence (
	concept_version_id CHAR(36) NOT NULL, 
	chunk_id CHAR(36) NOT NULL, 
	quote TEXT, 
	PRIMARY KEY (concept_version_id, chunk_id), 
	FOREIGN KEY(chunk_id) REFERENCES material_chunks (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE learning_explanations (
	id CHAR(36) NOT NULL, 
	user_id CHAR(36) NOT NULL, 
	concept_id CHAR(36) NOT NULL, 
	concept_version_id CHAR(36) NOT NULL, 
	material_id CHAR(36) NOT NULL, 
	explanation_language VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	latest_job_id CHAR(36) NOT NULL, 
	content_title VARCHAR(200), 
	content_markdown LONGTEXT, 
	created_at DATETIME(3) NOT NULL, 
	generated_at DATETIME(3), 
	PRIMARY KEY (id), 
	CHECK (explanation_language IN ('ko', 'en')), 
	FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(concept_version_id, concept_id) REFERENCES concept_versions (id, concept_id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(concept_id, material_id) REFERENCES concepts (id, material_id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	CHECK (generated_at IS NULL OR generated_at >= created_at), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	CHECK (status IN ('generating', 'ready', 'failed', 'outcome_unknown')), 
	FOREIGN KEY(latest_job_id) REFERENCES ai_jobs (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (latest_job_id), 
	UNIQUE (id, concept_version_id), 
	CHECK ((status = 'ready' AND content_title IS NOT NULL AND char_length(content_title) BETWEEN 1 AND 200 AND content_markdown IS NOT NULL AND char_length(content_markdown) BETWEEN 1 AND 24000 AND generated_at IS NOT NULL) OR (status <> 'ready' AND content_title IS NULL AND content_markdown IS NULL AND generated_at IS NULL))
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE question_concepts (
	question_id CHAR(36) NOT NULL, 
	concept_id CHAR(36) NOT NULL, 
	concept_version_id CHAR(36) NOT NULL, 
	PRIMARY KEY (question_id, concept_id), 
	FOREIGN KEY(concept_version_id, concept_id) REFERENCES concept_versions (id, concept_id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (question_id, concept_version_id), 
	FOREIGN KEY(question_id) REFERENCES questions (id) ON DELETE RESTRICT ON UPDATE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE question_set_concepts (
	set_id CHAR(36) NOT NULL, 
	concept_id CHAR(36) NOT NULL, 
	concept_version_id CHAR(36) NOT NULL, 
	PRIMARY KEY (set_id, concept_id), 
	FOREIGN KEY(concept_version_id, concept_id) REFERENCES concept_versions (id, concept_id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(concept_version_id) REFERENCES concept_versions (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(set_id) REFERENCES question_sets (id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	UNIQUE (set_id, concept_version_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE learning_explanation_evidence (
	explanation_id CHAR(36) NOT NULL, 
	concept_version_id CHAR(36) NOT NULL, 
	chunk_id CHAR(36) NOT NULL, 
	excerpt TEXT NOT NULL, 
	PRIMARY KEY (explanation_id, chunk_id), 
	CHECK (char_length(excerpt) BETWEEN 1 AND 2000), 
	FOREIGN KEY(explanation_id, concept_version_id) REFERENCES learning_explanations (id, concept_version_id) ON DELETE RESTRICT ON UPDATE RESTRICT, 
	FOREIGN KEY(concept_version_id, chunk_id) REFERENCES concept_version_evidence (concept_version_id, chunk_id) ON DELETE RESTRICT ON UPDATE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin;
