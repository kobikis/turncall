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
# A PBX reached over ARI (#209): no cloud carrier in between, and its edge is
# an ARI client rather than webhook routes.
ASTERISK = "asterisk"

CARRIERS: tuple[str, ...] = (TWILIO, TELNYX, ASTERISK)
DEFAULT_CARRIER = TWILIO

# The carriers whose numbers TurnCall searches, buys and releases on a
# project's account (ADR-0025). Not Asterisk: the PBX's dialplan owns its
# numbers.
PURCHASE_CARRIERS: tuple[str, ...] = (TWILIO, TELNYX)
# The kinds of number a search asks for. Not every carrier sells every kind in
# every country.
NUMBER_TYPES: tuple[str, ...] = ("local", "toll_free", "mobile")

# Which account a number was bought on (`purchased_numbers.account`): the
# platform's shared one (TWILIO_* without a pair; TELNYX_API_KEY when the
# project holds no `telnyx` credential), or the project's own. A release on
# the platform's account is allowed only for a number the project bought.
PLATFORM_ACCOUNT = "platform"
OWN_ACCOUNT = "own"
NUMBER_ACCOUNTS: tuple[str, ...] = (PLATFORM_ACCOUNT, OWN_ACCOUNT)
