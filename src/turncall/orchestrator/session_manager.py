"""Registry of active call sessions."""

from uuid import UUID

from loguru import logger

from turncall.orchestrator.call_session import CallSession


class SessionManager:
    """Manages active CallSessions across all concurrent calls.

    Thread-safe for asyncio (single-threaded event loop).
    """

    def __init__(self) -> None:
        self._sessions: dict[UUID, CallSession] = {}

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def register(self, session: CallSession) -> None:
        """Register a new call session."""
        self._sessions[session.call_id] = session

    def get(self, call_id: UUID) -> CallSession | None:
        """Get an active session by call ID."""
        return self._sessions.get(call_id)

    def remove(self, call_id: UUID) -> None:
        """Remove a session from the registry."""
        self._sessions.pop(call_id, None)

    async def stop_all(self) -> None:
        """Stop all active sessions (for graceful shutdown)."""
        logger.info("stopping_all_sessions", count=self.active_count)
        for session in list(self._sessions.values()):
            try:
                await session.stop()
            except Exception:
                logger.exception(
                    "session_stop_error",
                    call_id=str(session.call_id),
                )
        self._sessions.clear()
