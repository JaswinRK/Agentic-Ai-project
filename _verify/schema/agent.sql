-- schema/agent.sql — MySQL. Run once as the `support` user:
--   mysql -u support -psupport agent < schema/agent.sql

CREATE TABLE IF NOT EXISTS thread (
    id          CHAR(36) PRIMARY KEY,
    student_id  VARCHAR(64) NOT NULL,
    created_at  DOUBLE NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS message (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    thread_id   CHAR(36) NOT NULL,
    seq         INT NOT NULL,
    role        VARCHAR(16) NOT NULL,
    text        MEDIUMTEXT NOT NULL,
    UNIQUE KEY uq_message (thread_id, seq),
    CONSTRAINT fk_message_thread FOREIGN KEY (thread_id) REFERENCES thread(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS run (
    id               CHAR(36) PRIMARY KEY,
    thread_id        CHAR(36) NOT NULL,
    status           VARCHAR(16) NOT NULL DEFAULT 'queued',
    model            VARCHAR(64) NOT NULL,
    attempts         INT NOT NULL DEFAULT 0,
    max_attempts     INT NOT NULL DEFAULT 3,
    available_at     DOUBLE NOT NULL,
    lease_owner      VARCHAR(64) NULL,
    lease_until      DOUBLE NULL,
    cancel_requested TINYINT(1) NOT NULL DEFAULT 0,
    error_code       VARCHAR(64) NULL,
    tokens_in        INT NOT NULL DEFAULT 0,
    tokens_out       INT NOT NULL DEFAULT 0,
    created_at       DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
    started_at       VARCHAR(32) NULL,
    finished_at      VARCHAR(32) NULL,
    CONSTRAINT fk_run_thread FOREIGN KEY (thread_id) REFERENCES thread(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS run_step (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    run_id      CHAR(36) NOT NULL,
    seq         INT NOT NULL,
    kind        VARCHAR(16) NOT NULL,
    tokens_in   INT NOT NULL DEFAULT 0,
    tokens_out  INT NOT NULL DEFAULT 0,
    text        MEDIUMTEXT NULL,
    tool_calls  MEDIUMTEXT NULL,
    UNIQUE KEY uq_run_step (run_id, seq),
    CONSTRAINT fk_run_step_run FOREIGN KEY (run_id) REFERENCES run(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS tool_call (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    run_step_id      INT NOT NULL,
    tool_name        VARCHAR(64) NOT NULL,
    args             MEDIUMTEXT NOT NULL,
    result           MEDIUMTEXT NOT NULL,
    ok               TINYINT(1) NOT NULL,
    latency_ms       INT NOT NULL,
    idempotency_key  VARCHAR(255) NULL,
    CONSTRAINT fk_tool_call_step FOREIGN KEY (run_step_id) REFERENCES run_step(id)
) ENGINE=InnoDB;