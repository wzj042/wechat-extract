"""以 Streamable HTTP 暴露 FastMCP：只绑定本机、Bearer 鉴权、显式放行 Docker 宿主名。"""

from __future__ import annotations

import hmac

DOCKER_HOSTS = ["127.0.0.1:*", "localhost:*", "host.docker.internal:*"]


def bearer_guard(app, token: str):
    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await app(scope, receive, send)  # lifespan 等非 HTTP 事件原样透传
    return guarded


def serve_http(server, host: str, port: int, token: str) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    if not token or len(token) < 16:
        raise SystemExit("HTTP 模式需要至少 16 字符的 Bearer token")
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=DOCKER_HOSTS,
        allowed_origins=["http://" + h for h in DOCKER_HOSTS])
    uvicorn.run(bearer_guard(server.streamable_http_app(), token), host=host, port=port, log_level="warning")
