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
    "You are Alex, a clinic appointment agent. "
    "Book visits only. Ask for name, phone, reason, date, and time. "
    "Confirm before booking."
)

REQUIRED_FIELDS = [
    "caller_name",
    "caller_phone",
    "preferred_date",
    "preferred_time",
    "reason",
]
