"""Telephony adapters."""

from turncall.adapters.telephony.base import TelephonyAdapter
from turncall.adapters.telephony.twilio_adapter import TwilioAdapter

__all__ = ["TelephonyAdapter", "TwilioAdapter"]
