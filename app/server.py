import asyncio
import hmac
import logging
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp import web

from .config import Config
from .endpoints import PLAYER_ORIGIN
from .hls import Resources, rewrite_playlist, validate_url
from .upstream import Resolver, UpstreamError, read_limited

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
CONFIG = web.AppKey("config", Config)
RESOLVER = web.AppKey("resolver", Resolver)
PROXY = web.AppKey("proxy", object)
RESOURCES = web.AppKey("resources", Resources)


class Proxy:
    def __init__(self, session, resolver, resources, config):
        self.session = session
        self.resolver = resolver
        self.resources = resources
        self.config = config

    async def open(self, url, *, method="GET", headers=None, decompress=False):
        for _ in range(4):
            validate_url(url, self.config.allowed_hosts)
            try:
                response = await self.session.request(method, url, headers=headers or {}, allow_redirects=False,
                                                      auto_decompress=decompress)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise UpstreamError("The video CDN could not be reached from this server.") from exc
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise UpstreamError("The video CDN returned an invalid redirect.")
                url = urljoin(url, location)
                continue
            return response
        raise UpstreamError("The video CDN returned too many redirects.")

    async def playlist(self, *, url=None, controls=None):
        token = await self.resolver.get()
        for attempt in range(2):
            target = token.update_url(url) if url else token.live_url()
            if controls:
                parts = urlsplit(target)
                pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in controls]
                target = urlunsplit(parts._replace(query=urlencode(pairs + list(controls.items()))))
            response = await self.open(target, decompress=True)
            try:
                if response.status in (401, 403) and attempt == 0:
                    response.close()
                    token = await self.resolver.get(force=True, rejected=token)
                    continue
                if response.status != 200:
                    raise UpstreamError(f"The video CDN returned HTTP {response.status}. Playback may be unavailable for this programme or network.")
                body = await read_limited(response, 2_000_000)
                try:
                    text = body.decode("utf-8-sig")
                except UnicodeError as exc:
                    raise UpstreamError("The video CDN returned an invalid playlist.") from exc
                return rewrite_playlist(text, str(response.url), self.resources, self.config.access_token)
            finally:
                response.close()
        raise UpstreamError("The video CDN rejected the refreshed playback token.")


@web.middleware
async def access_and_errors(request, handler):
    config = request.app[CONFIG]
    if config.access_token and request.path != "/healthz" and not request.path.startswith("/static/"):
        supplied = request.query.get("key", "")
        if not hmac.compare_digest(supplied.encode(), config.access_token.encode()):
            return web.json_response({"error": "Open the player URL with ?key=YOUR_ACCESS_TOKEN."}, status=401,
                                     headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    try:
        response = await handler(request)
    except UpstreamError as exc:
        LOG.warning("%s", exc)
        response = web.json_response({"error": str(exc)}, status=502)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        response = web.json_response({"error": "The upstream connection timed out or disconnected."}, status=502)
    if not response.prepared:
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def index(request):
    return web.Response(text=(STATIC / "index.html").read_text(), content_type="text/html")


async def static_file(request):
    name = request.match_info["name"]
    types = {"app.js": "application/javascript", "style.css": "text/css"}
    if name not in types:
        raise web.HTTPNotFound()
    return web.Response(body=(STATIC / name).read_bytes(), content_type=types[name])


async def health(request):
    # Liveness only; it must not repeatedly hit the broadcaster's API.
    return web.json_response({"ok": True})


async def status(request):
    return web.json_response(request.app[RESOLVER].status())


def hls_controls(request):
    return {k: v for k, v in request.query.items()
            if k in ("_HLS_msn", "_HLS_part", "_HLS_skip") and len(v) < 40}


async def live(request):
    text = await request.app[PROXY].playlist(controls=hls_controls(request))
    return web.Response(text=text, content_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


async def resource(request):
    item = request.app[RESOURCES].get(request.match_info["id"])
    if item is None:
        raise web.HTTPGone(text="This playback resource expired. Press Play again.")
    if item.playlist:
        text = await request.app[PROXY].playlist(url=item.url, controls=hls_controls(request))
        return web.Response(text=text, content_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    headers = {k: request.headers[k] for k in ("Range", "If-Range") if k in request.headers}
    upstream = await request.app[PROXY].open(item.url, method=request.method, headers=headers)
    try:
        if upstream.status not in (200, 206, 416):
            raise UpstreamError(f"A video resource returned HTTP {upstream.status}. Press Reconnect to reload the live stream.")
        copy_headers = {k: upstream.headers[k] for k in (
            "Content-Type", "Content-Length", "Content-Range", "Content-Encoding", "Accept-Ranges", "ETag", "Last-Modified"
        ) if k in upstream.headers}
        copy_headers.update({"Cache-Control": "private, max-age=5", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
        response = web.StreamResponse(status=upstream.status, headers=copy_headers)
        await response.prepare(request)
        if request.method != "HEAD":
            try:
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
            except (ConnectionError, aiohttp.ClientError, asyncio.TimeoutError):
                # Headers have already been sent. Abort; never append an error JSON to video bytes.
                if request.transport:
                    request.transport.close()
        return response
    finally:
        upstream.close()


async def clients(app):
    headers = {
        "User-Agent": app[CONFIG].user_agent,
        "Referer": PLAYER_ORIGIN + "/", "Origin": PLAYER_ORIGIN,
        "Accept-Encoding": "identity",
    }
    async with aiohttp.ClientSession(headers=headers, trust_env=True, auto_decompress=False,
                                     connector=aiohttp.TCPConnector(limit=32),
                                     timeout=aiohttp.ClientTimeout(total=40, connect=20, sock_read=30)) as session:
        app[RESOLVER] = Resolver(session, app[CONFIG])
        app[PROXY] = Proxy(session, app[RESOLVER], app[RESOURCES], app[CONFIG])
        yield


def create_app(config=None, *, resolver=None, proxy=None, resources=None):
    app = web.Application(middlewares=[access_and_errors], client_max_size=1024)
    app[CONFIG] = config or Config.from_env()
    app[RESOURCES] = resources or Resources(app[CONFIG].allowed_hosts)
    if resolver is not None and proxy is not None:
        app[RESOLVER], app[PROXY] = resolver, proxy
    else:
        app.cleanup_ctx.append(clients)
    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static_file)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/status", status)
    app.router.add_get("/live.m3u8", live)
    app.router.add_get("/hls/{id}/{name}", resource)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    # URLs can contain the optional access key; disable request URL logging.
    web.run_app(create_app(config), host="0.0.0.0", port=config.port, access_log=None)


if __name__ == "__main__":
    main()
