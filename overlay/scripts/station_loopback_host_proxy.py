#!/usr/bin/env python3
"""Loopback-only HTTP/WebSocket proxy that rewrites Host safely."""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from urllib.parse import urlsplit

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def validate_upstream(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("upstream must be loopback HTTP")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("upstream must be loopback HTTP without credentials or query")
    if parsed.path not in {"", "/"} or not parsed.port:
        raise ValueError("upstream must be loopback HTTP with an explicit port")
    return value.rstrip("/")


def upstream_headers(headers: Mapping[str, str], host: str) -> dict[str, str]:
    result = {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP and key.lower() != "host"}
    result["Host"] = host
    return result


def create_app(upstream: str, upstream_host: str):
    from aiohttp import ClientSession, WSMsgType, web

    upstream = validate_upstream(upstream)

    async def websocket_proxy(request):
        downstream = web.WebSocketResponse()
        await downstream.prepare(request)
        headers = upstream_headers(request.headers, upstream_host)
        headers.pop("Content-Length", None)
        session = ClientSession()
        try:
            async with session.ws_connect("ws" + upstream[4:] + request.raw_path, headers=headers) as upstream_ws:
                async def downstream_to_upstream():
                    async for message in downstream:
                        if message.type == WSMsgType.TEXT:
                            await upstream_ws.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            await upstream_ws.send_bytes(message.data)
                        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            break

                async def upstream_to_downstream():
                    async for message in upstream_ws:
                        if message.type == WSMsgType.TEXT:
                            await downstream.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            await downstream.send_bytes(message.data)
                        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            break

                await asyncio.gather(downstream_to_upstream(), upstream_to_downstream())
        finally:
            await session.close()
            await downstream.close()
        return downstream

    async def http_proxy(request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await websocket_proxy(request)
        headers = upstream_headers(request.headers, upstream_host)
        body = await request.read()
        session = ClientSession()
        try:
            async with session.request(request.method, upstream + request.raw_path, headers=headers, data=body, allow_redirects=False) as response:
                response_headers = [(key.decode(), value.decode()) for key, value in response.raw_headers if key.decode().lower() not in _HOP_BY_HOP and key.decode().lower() != "content-length"]
                downstream = web.StreamResponse(status=response.status, reason=response.reason, headers=response_headers)
                await downstream.prepare(request)
                async for chunk in response.content.iter_chunked(65536):
                    await downstream.write(chunk)
                await downstream.write_eof()
                return downstream
        except Exception:
            return web.json_response({"detail": "loopback upstream unavailable"}, status=502)
        finally:
            await session.close()

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_route("*", "/{path:.*}", http_proxy)
    return app


def main() -> int:
    from aiohttp import web

    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1", choices=("127.0.0.1", "::1"))
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--upstream-host", required=True)
    args = parser.parse_args()
    if not 1 <= args.listen_port <= 65535:
        raise SystemExit("invalid listen port")
    app = create_app(args.upstream, args.upstream_host)
    web.run_app(app, host=args.listen_host, port=args.listen_port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
