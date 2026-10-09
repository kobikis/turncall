"""The carriers a phone number can belong to (ADR-0022).

A [[carrier]] connects a number's calls to the phone network. It is stored as
`phone_numbers.provider` and `calls.provider` (the columns keep their names; a
rename is a migration for no behaviour) and exposed by the API as `carrier`,
because "provider" means an STT/LLM/TTS backend everywhere else.

Adding one is an adapter, an inbound edge and a serializer: the media entry
point refuses a call whose carrier has no serializer, and
`tests/unit/test_carrier_boundary.py` fails until it has one.
"""

TWILIO = "twilio"
TELNYX = "telnyx"

CARRIERS: tuple[str, ...] = (TWILIO, TELNYX)
DEFAULT_CARRIER = TWILIO
