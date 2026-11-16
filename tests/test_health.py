import socket

import aiohttp

from health import start_health_server


async def test_no_port_means_no_server(monkeypatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    assert await start_health_server() is None


async def test_serves_ok_on_port(monkeypatch) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("PORT", str(port))
    runner = await start_health_server()
    assert runner is not None
    try:
        async with aiohttp.ClientSession() as http:
            for path in ("/", "/health"):
                async with http.get(f"http://127.0.0.1:{port}{path}") as r:
                    assert r.status == 200 and await r.text() == "ok"
    finally:
        await runner.cleanup()
