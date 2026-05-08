-- Schema for personaplex voice agent
-- MySQL 8+

CREATE TABLE IF NOT EXISTS agent_calls (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    call_uuid       VARCHAR(255) NOT NULL,
    caller_number   VARCHAR(50),
    called_number   VARCHAR(50),
    domain          VARCHAR(100) NOT NULL DEFAULT 'appointment',
    voice_prompt    VARCHAR(100),
    text_prompt     TEXT,
    status          ENUM('active','completed','failed') NOT NULL DEFAULT 'active',
    transcript      LONGTEXT          COMMENT 'Agent-side text tokens from PersonaPlex',
    summary         TEXT,
    started_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at        DATETIME,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_call_uuid (call_uuid),
    INDEX idx_status (status),
    INDEX idx_domain (domain)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_collected_data (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    call_id         BIGINT NOT NULL,
    field_name      VARCHAR(100) NOT NULL,
    field_value     TEXT,
    confirmed       BOOLEAN DEFAULT FALSE,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_call_field (call_id, field_name),
    FOREIGN KEY (call_id) REFERENCES agent_calls(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS appointment_bookings (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    call_id           BIGINT NOT NULL,
    caller_name       VARCHAR(255),
    caller_phone      VARCHAR(50),
    appointment_date  DATE,
    appointment_time  TIME,
    slot_label        VARCHAR(100),
    reason            TEXT,
    status            ENUM('pending','confirmed','cancelled') NOT NULL DEFAULT 'pending',
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_date (appointment_date),
    INDEX idx_status (status),
    FOREIGN KEY (call_id) REFERENCES agent_calls(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
