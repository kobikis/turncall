"""FastAPI dependency type aliases.

Use these Annotated types in route signatures instead of raw Depends().
This satisfies B008 (no function calls in defaults) and keeps routes clean.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.database import get_session

DbSession = Annotated[AsyncSession, Depends(get_session)]
