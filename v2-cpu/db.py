"""Database operations for the personaplex voice agent."""

import os
import threading

import pymysql
from dotenv import load_dotenv

from logging_config import get_logger

load_dotenv()
log = get_logger("agent.db")

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "agent_db")
DB_USER = os.getenv("DB_USER", "api_user")
DB_PASS = os.getenv("DB_PASS", "")

_schema_lock = threading.Lock()
_conversation_schema_ready = False

_CONVERSATION_REVIEW_COLUMNS = {
    "corrected_text": "LONGTEXT NULL",
    "review_label": "VARCHAR(100) NULL",
    "reviewer_notes": "TEXT NULL",
    "include_in_training": "BOOLEAN DEFAULT FALSE",
    "reviewed_at": "DATETIME NULL",
    "reviewed_by": "VARCHAR(100) NULL",
    "memory_scope": "ENUM('none','current_call','customer_fact','training_only','discard') NOT NULL DEFAULT 'none'",
    "memory_summary": "TEXT NULL",
    "use_in_live_context": "BOOLEAN DEFAULT FALSE",
    "memory_expires_at": "DATETIME NULL",
}

_CONVERSATION_REVIEW_INDEXES = {
    "idx_training": "ADD INDEX idx_training (include_in_training, role)",
    "idx_review_label": "ADD INDEX idx_review_label (review_label)",
    "idx_memory_scope": "ADD INDEX idx_memory_scope (memory_scope)",
    "idx_live_memory": "ADD INDEX idx_live_memory (use_in_live_context, memory_scope)",
}


def get_conn(autocommit=True):
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, db=DB_NAME,
        user=DB_USER, password=DB_PASS,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=autocommit,
        read_timeout=30, write_timeout=30,
    )


def ensure_conversation_review_schema(conn=None):
    """Create/upgrade conversation review columns for existing DB volumes."""
    global _conversation_schema_ready
    if _conversation_schema_ready:
        return

    owns_conn = conn is None
    with _schema_lock:
        if _conversation_schema_ready:
            return
        conn = conn or get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
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
                        corrected_text  LONGTEXT,
                        review_label    VARCHAR(100),
                        reviewer_notes  TEXT,
                        include_in_training BOOLEAN DEFAULT FALSE,
                        reviewed_at     DATETIME,
                        reviewed_by     VARCHAR(100),
                        memory_scope    ENUM('none','current_call','customer_fact','training_only','discard') NOT NULL DEFAULT 'none',
                        memory_summary  TEXT,
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
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
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
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    SELECT COLUMN_NAME
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='conversation_turns'
                    """,
                    (DB_NAME,),
                )
                existing = {row["COLUMN_NAME"] for row in cur.fetchall()}
                for column, definition in _CONVERSATION_REVIEW_COLUMNS.items():
                    if column not in existing:
                        cur.execute(
                            f"ALTER TABLE conversation_turns ADD COLUMN {column} {definition}"
                        )
                cur.execute(
                    """
                    SELECT INDEX_NAME
                    FROM INFORMATION_SCHEMA.STATISTICS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='conversation_turns'
                    """,
                    (DB_NAME,),
                )
                existing_indexes = {row["INDEX_NAME"] for row in cur.fetchall()}
                for index_name, definition in _CONVERSATION_REVIEW_INDEXES.items():
                    if index_name not in existing_indexes:
                        cur.execute(f"ALTER TABLE conversation_turns {definition}")
            _conversation_schema_ready = True
        finally:
            if owns_conn:
                conn.close()


def create_call(call_uuid, caller_number, called_number, domain, voice_prompt, text_prompt):
    conn = get_conn()
    try:
        try:
            ensure_conversation_review_schema(conn)
        except Exception:
            log.warning("Conversation review schema check failed", exc_info=True)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_calls "
                "(call_uuid, caller_number, called_number, domain, voice_prompt, text_prompt) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (call_uuid, caller_number, called_number, domain, voice_prompt, text_prompt),
            )
            return cur.lastrowid
    finally:
        conn.close()


def end_call(call_id, status="completed", transcript=None, summary=None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_calls SET status=%s, transcript=%s, summary=%s, "
                "ended_at=NOW(), updated_at=NOW() WHERE id=%s",
                (status, transcript, summary, call_id),
            )
    finally:
        conn.close()


def save_collected_data(call_id, field_name, field_value, confirmed=False):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_collected_data (call_id, field_name, field_value, confirmed) "
                "VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE field_value=%s, confirmed=%s, updated_at=NOW()",
                (call_id, field_name, field_value, confirmed, field_value, confirmed),
            )
    finally:
        conn.close()


def save_appointment(call_id, data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO appointment_bookings "
                "(call_id, caller_name, caller_phone, appointment_date, "
                " appointment_time, slot_label, reason, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'confirmed')",
                (
                    call_id,
                    data.get("caller_name"),
                    data.get("caller_phone"),
                    data.get("preferred_date"),
                    data.get("preferred_time"),
                    data.get("slot_label"),
                    data.get("reason"),
                ),
            )
            return cur.lastrowid
    finally:
        conn.close()

def save_conversation_turn(
    call_id,
    turn_index,
    role,
    text,
    intent=None,
    is_off_topic=False,
    source_used=None,
    quality_label=None,
):
    conn = get_conn()
    try:
        ensure_conversation_review_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_turns "
                "(call_id, turn_index, role, text, intent, is_off_topic, source_used, quality_label) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    call_id,
                    turn_index,
                    role,
                    text,
                    intent,
                    is_off_topic,
                    source_used,
                    quality_label,
                ),
            )
            return cur.lastrowid
    finally:
        conn.close()


def review_conversation_turn(
    turn_id,
    corrected_text=None,
    review_label=None,
    reviewer_notes=None,
    include_in_training=False,
    reviewed_by=None,
    memory_scope=None,
    memory_summary=None,
    use_in_live_context=False,
    memory_expires_at=None,
):
    conn = get_conn()
    try:
        ensure_conversation_review_schema(conn)
        memory_scope = memory_scope or ("training_only" if include_in_training else "none")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversation_turns SET "
                "corrected_text=%s, review_label=%s, reviewer_notes=%s, "
                "include_in_training=%s, reviewed_by=%s, reviewed_at=NOW(), "
                "memory_scope=%s, memory_summary=%s, use_in_live_context=%s, memory_expires_at=%s "
                "WHERE id=%s",
                (
                    corrected_text,
                    review_label,
                    reviewer_notes,
                    include_in_training,
                    reviewed_by,
                    memory_scope,
                    memory_summary,
                    use_in_live_context,
                    memory_expires_at,
                    turn_id,
                ),
            )
    finally:
        conn.close()


def update_turn_memory_policy(
    turn_id,
    memory_scope="none",
    memory_summary=None,
    use_in_live_context=False,
    memory_expires_at=None,
):
    conn = get_conn()
    try:
        ensure_conversation_review_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversation_turns SET "
                "memory_scope=%s, memory_summary=%s, use_in_live_context=%s, "
                "memory_expires_at=%s WHERE id=%s",
                (
                    memory_scope,
                    memory_summary,
                    use_in_live_context,
                    memory_expires_at,
                    turn_id,
                ),
            )
    finally:
        conn.close()


def save_customer_memory_fact(
    caller_number,
    fact_key,
    fact_value,
    source_call_id=None,
    source_turn_id=None,
    confidence=None,
):
    conn = get_conn()
    try:
        ensure_conversation_review_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO customer_memory_facts "
                "(caller_number, fact_key, fact_value, source_call_id, source_turn_id, confidence) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "fact_value=%s, source_call_id=%s, source_turn_id=%s, "
                "confidence=%s, is_active=TRUE, updated_at=NOW()",
                (
                    caller_number,
                    fact_key,
                    fact_value,
                    source_call_id,
                    source_turn_id,
                    confidence,
                    fact_value,
                    source_call_id,
                    source_turn_id,
                    confidence,
                ),
            )
            return cur.lastrowid
    finally:
        conn.close()


def get_customer_memory_facts(caller_number, limit=10):
    conn = get_conn()
    try:
        ensure_conversation_review_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fact_key, fact_value, confidence, updated_at "
                "FROM customer_memory_facts "
                "WHERE caller_number=%s AND is_active=TRUE "
                "ORDER BY updated_at DESC LIMIT %s",
                (caller_number, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()
