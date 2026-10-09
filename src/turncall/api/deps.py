"""FastAPI dependency type aliases.

Use these Annotated types in route signatures instead of raw Depends().
This satisfies B008 (no function calls in defaults) and keeps routes clean.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.database import get_session

# scope="function": the session commits when the endpoint returns, *before* the
# response is sent. FastAPI's default runs a yield dependency's exit after the
# response, so a client acting on it at once — Twilio opening the media stream
# its TwiML names — could read the database before the commit and find no row.
DbSession = Annotated[AsyncSession, Depends(get_session, scope="function")]
