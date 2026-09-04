"""Twilio webhook signature validation."""

from twilio.request_validator import RequestValidator


def validate_twilio_signature(
    auth_token: str,
    signature: str,
    url: str,
    params: dict[str, str],
) -> bool:
    """Validate a Twilio webhook signature against the auth token."""
    validator = RequestValidator(auth_token)
    return validator.validate(url, params, signature)
