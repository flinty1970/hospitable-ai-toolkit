"""One public port for webhook/admin and optional property-scoped MCP."""
import os
import asyncio
import requests
from fastapi import HTTPException
from hosting.config import load_registry
from contextlib import AsyncExitStack, asynccontextmanager
from hosting.gateway import create_app as gateway_app


def create_app():
    app = gateway_app()
    gateway_lifespan = app.router.lifespan_context
    accounts = load_registry(os.environ["TOOLKIT_ACCOUNTS_FILE"])

    @app.get("/ready")
    async def ready():
        def workers_ready():
            try:
                for account in accounts.values():
                    for prop in account["properties"].values():
                        response = requests.get(prop["worker_url"] + "/health", timeout=2, allow_redirects=False)
                        if response.status_code != 200 or not response.json().get("ok"):
                            return False
                return True
            except Exception:
                return False
        if not await asyncio.to_thread(workers_ready):
            raise HTTPException(503, "Property worker unavailable")
        return {"ok": True}

    from hosting.document_ui import install
    install(app, accounts)
    from hosting.admin_ui import install as install_admin
    install_admin(app, accounts)

    from hosting.scheduled_ui import install as install_scheduled
    install_scheduled(app, accounts)

    if os.environ.get("TOOLKIT_MCP_ENABLED") == "true":
        from hosting.mcp_server import create_app as mcp_app
        mcp = mcp_app()
        app.mount("/", mcp)

        @asynccontextmanager
        async def lifespan(application):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(gateway_lifespan(application))
                await stack.enter_async_context(mcp.router.lifespan_context(mcp))
                yield

        app.router.lifespan_context = lifespan
    return app
