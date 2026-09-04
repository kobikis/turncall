"""Authentication context carried through request lifecycle."""

from dataclasses import dataclass
from uuid import UUID

from turncall.domain.enums import ProjectRole


@dataclass(frozen=True)
class AuthContext:
    """Immutable auth context resolved from API key."""

    project_id: UUID
    api_key_id: UUID
    role: ProjectRole
    environment: str | None = None

    def has_role(self, *roles: ProjectRole) -> bool:
        return self.role in roles

    def can_write(self) -> bool:
        return self.role in (ProjectRole.ADMIN, ProjectRole.DEVELOPER)

    def can_admin(self) -> bool:
        return self.role == ProjectRole.ADMIN
