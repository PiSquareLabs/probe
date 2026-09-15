"""Probe — configure Elasticsearch's Jira integrations from one UI.

Entry point. Wires the routers, CORS and a uniform error envelope so the React
client can render any failure without special-casing each endpoint.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .errors import ProbeError
from .routers import connectors, health, jira, setup, tickets

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

DESCRIPTION = """
Configure Elasticsearch's Jira integrations from one place, without opening Kibana.

One set of Jira credentials, entered here, provisions:

* the **Elasticsearch content connector** (`service_type: jira`) that syncs
  tickets into an index for search and duplicate detection, and
* the **Kibana action connector** (`.jira`) that creates tickets — where the
  Elastic licence permits it.

The Jira action connector requires a **Gold** licence. On Basic the app creates
tickets directly through the Jira REST API instead, using the same credentials.
See `GET /api/connector/capabilities` for what this deployment supports.
""".strip()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Probe — Elasticsearch + Jira control plane",
        description=DESCRIPTION,
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ProbeError)
    async def probe_error_handler(_: Request, exc: ProbeError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    for router in (health.router, setup.router, connectors.router, jira.router, tickets.router):
        app.include_router(router)

    return app


app = create_app()
