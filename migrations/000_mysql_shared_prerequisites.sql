-- Fresh empty database prerequisites for the canonical 000..005 chain.
-- Never run against an existing database: users and plan_tasks must be absent.
-- The empty plan_tasks ID stub is expanded in place by 005.

CREATE TABLE users (
	id CHAR(36) NOT NULL,
	auth_issuer VARCHAR(255) NOT NULL,
	auth_subject VARCHAR(255) NOT NULL,
	email VARCHAR(320),
	display_name VARCHAR(120),
	timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Seoul',
	created_at DATETIME(3) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (auth_issuer, auth_subject),
	CHECK (timezone = 'Asia/Seoul')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE plan_tasks (
    id CHAR(36) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
