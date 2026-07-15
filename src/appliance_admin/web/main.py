from __future__ import annotations

import uvicorn

from appliance_admin.config import WebSettings


def main() -> None:
    settings = WebSettings()
    uvicorn.run(
        "appliance_admin.web.app:app",
        host=settings.bind_host,
        port=settings.bind_port,
        proxy_headers=settings.trusted_proxy_headers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
