import logging
import os

from aiohttp import web

logger = logging.getLogger(__name__)


async def start_health_server() -> web.AppRunner | None:
    """Открывает порт из $PORT и отвечает «ok». Render (Web Service) ждёт порт, иначе
    деплой висит в «In progress»; внешний пинг этого адреса не даёт бесплатному сервису заснуть."""
    port = os.environ.get("PORT")
    if not port:
        return None  # локально и на Background Worker порт не нужен
    app = web.Application()

    async def ok(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    logger.info("Порт %s открыт для проверки Render", port)
    return runner
