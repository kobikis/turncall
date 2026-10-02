"""V1 API request/response schemas."""

# Every phone number the API accepts; one pattern, so a suppressed number and a
# dialled one compare as the same string (#227).
E164_PATTERN = r"^\+[1-9]\d{1,14}$"
