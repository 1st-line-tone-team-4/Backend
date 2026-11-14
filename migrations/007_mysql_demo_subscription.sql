-- Apply after 006. Contract 1.5.0 stores a per-learner demo subscription state
-- (pricing_pending = free, active = subscribed). No payment data is stored.
CREATE TABLE user_subscriptions (
	user_id CHAR(36) NOT NULL,
	status VARCHAR(32) NOT NULL DEFAULT 'pricing_pending',
	updated_at DATETIME(3) NOT NULL,
	PRIMARY KEY (user_id),
	CONSTRAINT fk_user_subscriptions_0 FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
	CONSTRAINT ck_user_subscriptions_0 CHECK (status IN ('pricing_pending','active'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
