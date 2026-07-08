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

CREATE TABLE IF NOT EXISTS conversation_turns (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    call_id         BIGINT NOT NULL,
    turn_index      INT NOT NULL,
    role            ENUM('caller','ai','system') NOT NULL,
    text            LONGTEXT NOT NULL,
    intent          VARCHAR(100),
    is_off_topic    BOOLEAN DEFAULT FALSE,
    source_used     VARCHAR(100),
    quality_label   VARCHAR(100),
    corrected_text   LONGTEXT,
    review_label     VARCHAR(100),
    reviewer_notes   TEXT,
    include_in_training BOOLEAN DEFAULT FALSE,
    reviewed_at      DATETIME,
    reviewed_by      VARCHAR(100),
    memory_scope     ENUM('none','current_call','customer_fact','training_only','discard') NOT NULL DEFAULT 'none',
    memory_summary   TEXT,
    use_in_live_context BOOLEAN DEFAULT FALSE,
    memory_expires_at DATETIME,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_call_turn (call_id, turn_index),
    INDEX idx_role (role),
    INDEX idx_training (include_in_training, role),
    INDEX idx_review_label (review_label),
    INDEX idx_memory_scope (memory_scope),
    INDEX idx_live_memory (use_in_live_context, memory_scope),
    FOREIGN KEY (call_id) REFERENCES agent_calls(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS customer_memory_facts (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    caller_number   VARCHAR(50) NOT NULL,
    fact_key        VARCHAR(100) NOT NULL,
    fact_value      TEXT NOT NULL,
    source_call_id  BIGINT,
    source_turn_id  BIGINT,
    confidence      DECIMAL(5,4),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_caller_fact (caller_number, fact_key),
    INDEX idx_caller_active (caller_number, is_active),
    FOREIGN KEY (source_call_id) REFERENCES agent_calls(id) ON DELETE SET NULL,
    FOREIGN KEY (source_turn_id) REFERENCES conversation_turns(id) ON DELETE SET NULL
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
