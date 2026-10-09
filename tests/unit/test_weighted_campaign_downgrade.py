"""Downgrading past weighted campaigns refuses rather than deletes them (#234).

The revision before f7c3d5e9a1b4 has no room for a campaign without one
agent. Deleting those campaigns — and, by cascade, every target and its
attempt history — on a rollback run under pressure is the wrong default: the
downgrade stops and says what to do instead.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic/versions/f7c3d5e9a1b4_campaign_weighted_agents.py"
)


class _Op:
    def __init__(self, weighted: int) -> None:
        self.weighted = weighted
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def get_bind(self) -> Any:
        weighted = self.weighted
        return SimpleNamespace(
            execute=lambda _sql: SimpleNamespace(scalar_one=lambda: weighted)
        )

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: self.calls.append((name, args))


def _migration(op: _Op) -> Any:
    spec = importlib.util.spec_from_file_location("weighted_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = op
    return module


def test_refuses_while_a_weighted_campaign_exists():
    op = _Op(weighted=2)
    with pytest.raises(RuntimeError, match="2 weighted campaign"):
        _migration(op).downgrade()
    assert op.calls == []


def test_downgrades_when_there_is_none():
    op = _Op(weighted=0)
    _migration(op).downgrade()
    names = [name for name, _ in op.calls]
    assert "execute" not in names
    assert names == ["drop_column", "drop_column", "alter_column"]
