"""
Appointment booking domain for PersonaPlex.

Defines the text prompt (persona) that conditions PersonaPlex's behavior.
PersonaPlex uses a TEXT PROMPT for role/behavior and a VOICE PROMPT (.pt) for voice.
"""

DOMAIN_NAME = "appointment"
DEFAULT_VOICE_PROMPT = "NATF2.pt"

# Text prompt in PersonaPlex's customer-service style.
# PersonaPlex wraps this in <system> tags internally.
TEXT_PROMPT = "If caller says congratulations, say thank you."

REQUIRED_FIELDS = [
    "caller_name",
    "caller_phone",
    "preferred_date",
    "preferred_time",
    "reason",
]
