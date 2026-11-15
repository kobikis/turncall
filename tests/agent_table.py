"""An in-memory agent table for unit tests that read agents by id.

`agent_repo.get_agent_by_id` and `knowledge_repo.kb_modes_by_agent` over a
dict, scoped by project the way the repository scopes them. The modules'
functions are replaced, so every importer — agent acceptance, the call-init
resolver — reads the same table.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import pytest

from turncall.storage.repositories import agent_repo, knowledge_repo


def agent_table(monkeypatch: pytest.MonkeyPatch) -> dict[uuid.UUID, Any]:
    """The table, empty: add `agent_row(...)`s to it by id."""
    rows: dict[uuid.UUID, Any] = {}

    async def get_agent_by_id(_session: Any, agent_id: uuid.UUID, *, project_id=None):
        row = rows.get(agent_id)
        if row is None or (project_id is not None and row.project_id != project_id):
            return None
        return row

    async def kb_modes_by_agent(_session: Any, agent_ids: list[uuid.UUID]):
        return {
            agent_id: rows[agent_id].kb_modes
            for agent_id in agent_ids
            if rows[agent_id].kb_modes
        }

    monkeypatch.setattr(agent_repo, "get_agent_by_id", get_agent_by_id)
    monkeypatch.setattr(knowledge_repo, "kb_modes_by_agent", kb_modes_by_agent)
    return rows


def agent_row(
    project_id: uuid.UUID,
    config: dict[str, Any] | None = None,
    kb_modes: Iterable[str] = (),
    name: str = "support",
) -> Any:
    """A stored agent of `project_id`, with its linked knowledge-base modes."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        config_blob=config or {},
        kb_modes=list(kb_modes),
    )
