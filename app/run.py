"""Process entry point that honors validated listener settings."""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    target = (
        "app.gateway_repo_main:app"
        if settings.service_profile == "gateway_repository"
        else "app.main:app"
    )
    uvicorn.run(
        target,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
