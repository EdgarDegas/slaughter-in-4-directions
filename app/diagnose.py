"""Run from the home server: python -m app.diagnose."""

import asyncio
import re
import sys
from urllib.parse import urlsplit

from aiohttp import web

from .server import create_app, PROXY, RESOLVER, RESOURCES
from .upstream import UpstreamError


async def diagnose():
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        print("1/3 Checking SMG API and playback credentials…", flush=True)
        token = await app[RESOLVER].get()
        print(f"    OK: channel {app[RESOLVER].config.channel_id}, stream {token.stream}", flush=True)
        print("2/3 Checking HLS playlists…", flush=True)
        text = await app[PROXY].playlist()
        for _ in range(5):
            lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
            if not lines:
                raise UpstreamError("The playlist is empty.")
            match = re.match(r"/hls/([^/]+)/", lines[0])
            if not match:
                raise UpstreamError("The playlist was not rewritten correctly.")
            resource = app[RESOURCES].get(match[1])
            if resource.playlist:
                text = await app[PROXY].playlist(url=resource.url)
                continue
            print("    OK: media playlist received", flush=True)
            print("3/3 Checking a video segment…", flush=True)
            response = await app[PROXY].open(resource.url, headers={"Range": "bytes=0-1023"})
            try:
                if response.status not in (200, 206):
                    raise UpstreamError(f"The media segment returned HTTP {response.status}.")
                sample = await response.content.read(1024)
                if not sample:
                    raise UpstreamError("The video segment is empty.")
                print(f"    OK: received {len(sample)} bytes from {urlsplit(resource.url).hostname}", flush=True)
            finally:
                response.close()
            print("Network playback checks passed. Next: test Safari playback and AirPlay on your devices.")
            return 0
        raise UpstreamError("Too many nested playlists.")
    except (UpstreamError, asyncio.TimeoutError) as exc:
        print("FAILED: " + (str(exc) or "Upstream request timed out."), flush=True)
        return 1
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    sys.exit(asyncio.run(diagnose()))
