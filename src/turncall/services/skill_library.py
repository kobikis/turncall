"""Where a project's skills come from (#432, ADR-0031).

A project may be linked to a **skill library**: one other project, whose
skills it resolves instead of its own. One rule, `skill_source`, decides it —
the library when the project is linked, else the project itself — and every
place a skill name is resolved goes through it: an agent's save checks, a
call's resolution (stored agent, call-init inline agent, handoff target), a
pin, a text session, an eval run, and a skill's knowledge bases. The library
is the only source for a linked project, never a fallback: a name resolving
in the project for one agent and in the library for another would mean two
things in one workspace, and only a call's record could say which ran.

The link is the platform's (`PUT /v1/projects/{id}/skill-library`, platform
credential only): no API key can make its project read another's skills. A
library is one level deep: it is never linked itself, and a project other
projects read from is never linked (`link_refusal`). A library that is gone —
soft-deleted — resolves nothing: the linked project's skills are warnings and
skipped skills, never a failed call. A purged one unlinks its projects (the
foreign key's `SET NULL`), which then read their own again.

`readers` is the rule inverted: the projects whose agents resolve skills in
a project — what a skill's delete refusal and agents listing span (#434).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.repositories import project_repo


@dataclass(frozen=True)
class SkillSource:
    """The project a project's skill names resolve in: `project_id` — None
    when it is linked to a library that is gone — and whether that is its
    library rather than itself."""

    project_id: UUID | None
    library: bool = False

    @property
    def where(self) -> str:
        """How a refusal or a warning names it."""
        return "this project's skill library" if self.library else "this project"


async def skill_source(session: AsyncSession, project_id: UUID) -> SkillSource:
    """Where `project_id`'s skills come from: its library when linked, else
    itself. One read."""
    link = await project_repo.skill_library_of(session, project_id)
    if link is None:
        return SkillSource(project_id)
    library_id, live = link
    return SkillSource(library_id if live else None, library=True)


async def readers(session: AsyncSession, project_id: UUID) -> list[UUID]:
    """The live projects whose agents resolve skills in `project_id`: the
    projects linked to it, and itself unless it is linked elsewhere."""
    own = await skill_source(session, project_id)
    linked = await project_repo.projects_linked_to(session, project_id)
    itself = [project_id] if not own.library else []
    return [*itself, *(p for p in linked if p != project_id)]


async def link_refusal(
    session: AsyncSession, project_id: UUID, library_id: UUID
) -> str | None:
    """Why `project_id` may not read its skills from `library_id`, or None.

    Never itself; never a project that is missing or soft-deleted; never one
    linked to a library of its own; and never into a project others read from
    — either way a chain, whose middle would be a library that reads another.
    """
    if library_id == project_id:
        return "A project cannot be its own skill library"
    library = await project_repo.get_project_by_id(session, library_id)
    if library is None:
        return f"Project '{library_id}' does not exist or is deleted"
    if library.skill_library_id is not None:
        return (
            f"Project '{library_id}' reads its skills from a library of its own; "
            "a skill library cannot be linked to another"
        )
    if await project_repo.projects_linked_to(session, project_id):
        return (
            "This project is the skill library of other projects; a skill "
            "library cannot be linked to another"
        )
    return None
