"""`turncall-demo`: the website's talk-to-an-agent server.

    DEMO_AGENTS_FILE=demo-agents.json OPENAI_API_KEY=... DEEPGRAM_API_KEY=... \
        turncall-demo

Refuses to start on an agents file it cannot run, so a bad deploy fails at
boot, not on a visitor's call.
"""

from __future__ import annotations

import sys

import uvicorn
from loguru import logger

from turncall.config.settings import get_settings
from turncall.demo.agents import DemoAgentError, load_demo_agents
from turncall.demo.app import create_demo_app


def main() -> None:
    settings = get_settings()
    try:
        agents = load_demo_agents(
            settings.demo.agents_file, max_call_seconds=settings.demo.max_call_seconds
        )
    except DemoAgentError as exc:
        logger.error("demo_agents_unusable: {err}", err=str(exc))
        sys.exit(2)
    logger.info("demo_agents_loaded", agents=list(agents))
    uvicorn.run(
        create_demo_app(agents, settings),
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
    )


if __name__ == "__main__":
    main()
