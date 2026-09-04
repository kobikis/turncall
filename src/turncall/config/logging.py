"""Logging configuration with loguru."""

import logging
import sys

from loguru import logger


def setup_logging(log_level: str = "info", json_output: bool = False) -> None:
    """Configure loguru for the application."""
    # Remove default handler
    logger.remove()

    # enqueue=True makes the sink async (logs go through a queue off the calling
    # thread) so a slow write — or a chatty pipeline observer — can't stall the
    # realtime audio path. See ADR-0010 / ADR-0004.
    if json_output:
        logger.add(
            sys.stdout,
            level=log_level.upper(),
            serialize=True,
            enqueue=True,
        )
    else:
        logger.add(
            sys.stdout,
            level=log_level.upper(),
            enqueue=True,
            format=(
                "<green>{time:HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
        )

    # Intercept stdlib logging (uvicorn, sqlalchemy) into loguru
    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            level: str | int
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            frame, depth = logging.currentframe(), 2
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
