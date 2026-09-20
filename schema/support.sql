-- schema/library.sql — MySQL. Run once as the `support` user:
--   mysql -u support -psupport support < schema/library.sql

CREATE TABLE IF NOT EXISTS account (
    id                 INT PRIMARY KEY,
    handle             VARCHAR(64)  NOT NULL UNIQUE,
    display_name       VARCHAR(128) NOT NULL,
    tier               VARCHAR(16)  NOT NULL,          -- free | plus | pro
    credits_used_today INT NOT NULL DEFAULT 0,
    credits_reset_at   DOUBLE NOT NULL DEFAULT 0,
    CONSTRAINT chk_credits_nonneg CHECK (credits_used_today >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS post (
    id          INT PRIMARY KEY,
    account_id  INT NOT NULL,
    body        TEXT NOT NULL,
    visibility  VARCHAR(16) NOT NULL DEFAULT 'public',  -- public | followers | private
    created_at  DOUBLE NOT NULL,
    CONSTRAINT fk_post_account FOREIGN KEY (account_id) REFERENCES account(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ticket (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    account_id  INT NOT NULL,
    issue_type  VARCHAR(32) NOT NULL,
    body        TEXT NOT NULL,
    status      VARCHAR(16) NOT NULL DEFAULT 'open',
    created_at  DOUBLE NOT NULL,
    open_slot   VARCHAR(16) GENERATED ALWAYS AS (IF(status = 'open', issue_type, NULL)) STORED,
    UNIQUE KEY uq_open_ticket (account_id, open_slot),
    CONSTRAINT fk_ticket_account FOREIGN KEY (account_id) REFERENCES account(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS policy (
    name   VARCHAR(64) PRIMARY KEY,
    value  INT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS reply (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    ticket_id   INT NOT NULL,
    body        VARCHAR(500) NOT NULL,
    created_at  DOUBLE NOT NULL,
    UNIQUE KEY uq_reply (ticket_id, body),
    CONSTRAINT fk_reply_ticket FOREIGN KEY (ticket_id) REFERENCES ticket(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS assignment (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    ticket_id   INT NOT NULL,
    moderator   VARCHAR(64) NOT NULL,
    created_at  DOUBLE NOT NULL,
    UNIQUE KEY uq_assignment_ticket (ticket_id),
    CONSTRAINT fk_assignment_ticket FOREIGN KEY (ticket_id) REFERENCES ticket(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS escalation (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    ticket_id   INT NOT NULL,
    reason      VARCHAR(200) NOT NULL,
    created_at  DOUBLE NOT NULL,
    UNIQUE KEY uq_esc_ticket (ticket_id),
    CONSTRAINT fk_esc_ticket FOREIGN KEY (ticket_id) REFERENCES ticket(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS outbox (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    recipient   VARCHAR(64) NOT NULL,
    channel     VARCHAR(16) NOT NULL,     -- dm | email | push
    body        VARCHAR(160) NOT NULL,
    dedupe_key  VARCHAR(255) NOT NULL UNIQUE,
    created_at  DOUBLE NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS idempotency (
    `key`       VARCHAR(255) PRIMARY KEY,
    tool_name   VARCHAR(64) NOT NULL,
    result      MEDIUMTEXT NOT NULL,
    created_at  DOUBLE NOT NULL
) ENGINE=InnoDB;