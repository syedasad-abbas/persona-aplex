"""
Appointment booking domain for PersonaPlex.

Defines the text prompt (persona) that conditions PersonaPlex's behavior.
PersonaPlex uses a TEXT PROMPT for role/behavior and a VOICE PROMPT (.pt) for voice.
"""

DOMAIN_NAME = "appointment"
DEFAULT_VOICE_PROMPT = "NATF2.pt"

# Keep this prompt compact: PersonaPlex replays one model step per text token
# before the websocket becomes ready, and CPU prewarm is very sensitive to length.
TEXT_PROMPT = """
Alex from HealthFirst. Ask name, then phone. Repeat each. Brief.
"""
