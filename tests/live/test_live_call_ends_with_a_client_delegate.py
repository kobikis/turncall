"""A call with an openai_live client delegate ends (#245).

The delegate runs in pipecat's backend worker, a child of the call's pipeline
worker. pipecat 1.12's runner waited on that child forever once the pipeline
ended — through the hangup gate's `EndWorkerFrame`, or a direct cancel on a
hangup — so the call was never finalized (call 8e3fef17 stayed
`in_progress`, no `call.ended`). `run_until_pipeline_done` cancels the runner
once the pipeline is done.

Live: the hang needs the real service connected (a stubbed connection did not
reproduce it). Needs OPENAI_API_KEY.
"""

import asyncio
import os

import pytest
from pipecat.frames.frames import EndWorkerFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.services.openai.live.llm import ClientDelegation, OpenAILiveLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.workers.llm.backend_llm_worker import BackendLLMWorker
from pipecat.workers.runner import WorkerRunner

from turncall.orchestrator.call_session import run_until_pipeline_done

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


@pytest.mark.parametrize("how", ["end_frame", "cancel"])
async def test_a_client_delegation_call_ends(how: str) -> None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("needs OPENAI_API_KEY")
    delegate = BackendLLMWorker(llm=OpenAILLMService(api_key=key))
    live = OpenAILiveLLMService(
        api_key=key, delegation=ClientDelegation(backend=delegate)
    )
    worker = PipelineWorker(Pipeline([live]), params=PipelineParams())
    run = asyncio.create_task(run_until_pipeline_done(WorkerRunner(), worker))
    await asyncio.sleep(4)  # connected
    if how == "end_frame":
        await worker.queue_frame(EndWorkerFrame(reason="assistant_ended"))
    else:
        await worker.cancel()
    await asyncio.wait_for(run, timeout=20)
