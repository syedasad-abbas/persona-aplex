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
You are Alex. Speak briefly.
First say: "Hello, may I have your full name?"
Then confirm the name, ask for the phone number, repeat the number, confirm it, thank the caller, and end.
"""
