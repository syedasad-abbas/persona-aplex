"""Database operations for the personaplex voice agent."""

import os

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


def get_conn(autocommit=True):
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, db=DB_NAME,
        user=DB_USER, password=DB_PASS,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=autocommit,
        read_timeout=30, write_timeout=30,
    )


def create_call(call_uuid, caller_number, called_number, domain, voice_prompt, text_prompt):
    conn = get_conn()
    try:
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
