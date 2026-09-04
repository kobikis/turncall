"""trigger_post_call_analysis keeps a strong reference to its fire-and-forget
task (review: asyncio holds only a weak ref -> the mandatory call.ended could
be GC'd mid-flight)."""

import asyncio
import uuid
from unittest.mock import patch

import pytest

from turncall.services import call_analysis_trigger


@pytest.mark.unit
@pytest.mark.asyncio
async def test_task_strongly_referenced_then_discarded():
    release = asyncio.Event()

    async def fake_run(*a, **k):
        await release.wait()

    with patch.object(call_analysis_trigger, "_run_post_call", new=fake_run):
        task = call_analysis_trigger.trigger_post_call_analysis(
            session_factory=lambda: None,
            call_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            agent_config_blob={},
        )
        # Registered while running — survives GC even if the caller drops `task`.
        assert task in call_analysis_trigger._POST_CALL_TASKS
        release.set()
        await task
        await asyncio.sleep(0)  # let the done-callback run
        assert task not in call_analysis_trigger._POST_CALL_TASKS
