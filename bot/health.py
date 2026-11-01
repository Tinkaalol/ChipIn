import os

from aiohttp import web


async def start_health_server() -> web.AppRunner | None:
    """Открывает порт из $PORT, если он задан: Render Web Service требует слушающий порт."""
    port = os.environ.get("PORT")
    if not port:
        return None

    async def ok(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    return runner
