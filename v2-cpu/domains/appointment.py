"""
Appointment booking domain for PersonaPlex.

Defines the text prompt (persona) that conditions PersonaPlex's behavior.
PersonaPlex uses a TEXT PROMPT for role/behavior and a VOICE PROMPT (.pt) for voice.
"""

DOMAIN_NAME = "appointment"
DEFAULT_VOICE_PROMPT = "NATF2.pt"

# Text prompt in PersonaPlex's customer-service style.
# PersonaPlex wraps this in <system> tags internally.
TEXT_PROMPT = (
    "You are Alex, HealthFirst Medical Center appointment agent. "
    "Book clinic visits only. Collect full name, phone, reason, date, and time. "
    "Slots: Mon 9AM 10AM 2PM; Tue 9AM 11AM 3PM; Wed 10AM 1PM 4PM; "
    "Thu 9AM 2PM 3PM; Fri 9AM 10AM 11AM. Confirm details before booking."
)

REQUIRED_FIELDS = [
    "caller_name",
    "caller_phone",
    "preferred_date",
    "preferred_time",
    "reason",
]
