"""Entry point for running the application directly."""

import uvicorn

from turncall.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "turncall.app:create_app",
        factory=True,
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
        reload=not settings.is_production,
    )


if __name__ == "__main__":
    main()
