import asyncio
import base64
import json
import re
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Config
from app.constants import USER_AGENT
from app.endpoints import STREAM_HOST
from app.hls import Resources, rewrite_playlist, validate_url
from app.server import Proxy, create_app
from app.upstream import Credentials, Resolver, UpstreamError, decode_address, sign_params

HOST = STREAM_HOST
BASE = f"https://{HOST}/live/sports/index.m3u8"


def credentials(label="old", life=3600):
    return Credentials("sports", label, "secret-" + label, "123", time.time() + life)


class ConfigTests(unittest.TestCase):
    def test_blank_environment_generates_anonymous_browser_identity(self):
        with patch.dict("os.environ", {"SMG_UUID": "", "SMG_USER_AGENT": "", "UPSTREAM_HOSTS": ""}, clear=True):
            first, second = Config.from_env(), Config.from_env()
        self.assertRegex(first.uuid, r"^[A-Za-z0-9_-]{21}$")
        self.assertNotEqual(first.uuid, second.uuid)
        self.assertEqual(first.user_agent, USER_AGENT)
        self.assertEqual(first.allowed_hosts, (STREAM_HOST,))

    def test_explicit_browser_identity_is_preserved(self):
        with patch.dict("os.environ", {"SMG_UUID": "a" * 21, "SMG_USER_AGENT": "test-browser", "UPSTREAM_HOSTS": " Media.Example.test "}, clear=True):
            config = Config.from_env()
        self.assertEqual(config.uuid, "a" * 21)
        self.assertEqual(config.user_agent, "test-browser")
        self.assertEqual(config.allowed_hosts, ("media.example.test",))


class HLSTests(unittest.TestCase):
    def test_signature_matches_independent_node_crypto_fixture(self):
        result = sign_params({"channel_program_id": 12345}, Config(), timestamp=1700000000, nonce="abcdefgh")
        self.assertEqual(result["sign"], "95a3f43e2842d2fe7b7113fbb37df03d")

    def test_public_rsa_recovers_multiple_wrapped_blocks(self):
        fixture = json.loads(Path(__file__).with_name("rsa_fixture.json").read_text())
        result = decode_address(fixture["wrapped"], modulus=int(fixture["modulus"], 16))
        self.assertEqual(result, base64.b64decode(fixture["expected_b64"]).decode())

    def test_rewrites_all_resource_types_and_keeps_range_tags(self):
        resources = Resources((HOST,))
        source = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="zh",URI="audio.m3u8"
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe.m3u8"
#EXT-X-KEY:METHOD=AES-128,URI="key.bin?x=1&y=2"
#EXT-X-MAP:URI="init.mp4",BYTERANGE="100@0"
#EXT-X-PART:DURATION=0.5,URI="part.m4s"
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="next.m4s"
#EXT-X-RENDITION-REPORT:URI="other.m3u8",LAST-MSN=1
#EXT-X-STREAM-INF:BANDWIDTH=123
variant.m3u8?token=private&volcTime=12
#EXT-X-BYTERANGE:4@0
segment.ts
'''
        result = rewrite_playlist(source, BASE, resources, "home-key")
        self.assertNotIn("https:", result)
        self.assertNotIn("private", result)
        self.assertIn('#EXT-X-BYTERANGE:4@0', result)
        self.assertIn('BYTERANGE="100@0"', result)
        self.assertEqual(len(resources.items), 9)
        self.assertEqual(result.count("?key=home-key"), 9)
        self.assertEqual(sum(x.playlist for x in resources.items.values()), 4)

    def test_variant_url_stays_stable_when_token_changes(self):
        r = Resources((HOST,))
        old = r.register(BASE + "?token=old&volcSecret=a&volcTime=1", True)
        new = r.register(BASE + "?token=new&volcSecret=b&volcTime=2", True)
        self.assertEqual(old, new)
        self.assertIn("token=new", r.get(old).url)

    def test_disallowed_urls_and_redirect_targets_fail_closed(self):
        for url in ["http://127.0.0.1/a", "https://127.0.0.1/a", "https://evil.com/a",
                    f"https://{HOST}.evil.com/a", f"https://x@{HOST}/a", f"https://{HOST}:8443/a",
                    "file:///etc/passwd", "data:text/plain,test"]:
            with self.subTest(url=url), self.assertRaises(UpstreamError):
                validate_url(url, (HOST,))
        with self.assertRaises(UpstreamError):
            rewrite_playlist('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="http://127.0.0.1/key"', BASE, Resources((HOST,)))

    def test_html_and_unsupported_variable_playlists_are_rejected(self):
        for text in ["<html>denied</html>", '#EXTM3U\n#EXT-X-DEFINE:NAME="x",VALUE="y"']:
            with self.assertRaises(UpstreamError):
                rewrite_playlist(text, BASE, Resources((HOST,)))

    def test_registry_is_bounded(self):
        r = Resources((HOST,), limit=2)
        first = r.register(BASE + "?x=1")
        r.register(BASE + "?x=2")
        r.register(BASE + "?x=3")
        self.assertIsNone(r.get(first))
        self.assertEqual(len(r.items), 2)

    def test_token_base64url_and_expiry(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode()).decode().rstrip("=")
        t = Credentials.from_url(BASE + f"?token=h.{payload}.s&volcSecret=abc&volcTime={int(time.time()) + 3600}")
        self.assertEqual(t.stream, "sports")
        with self.assertRaises(UpstreamError):
            Credentials.from_url(BASE + "?token=broken")
        self.assertNotIn("abc", repr(t))

    def test_cdn_expiry_limits_an_otherwise_valid_jwt(self):
        now = int(time.time())
        payload = base64.urlsafe_b64encode(json.dumps({"exp": now + 43200}).encode()).decode().rstrip("=")
        url = BASE + f"?token=h.{payload}.s&volcSecret=abc&volcTime="
        self.assertEqual(Credentials.from_url(url + str(now + 600)).expires, now + 600)
        with self.assertRaises(UpstreamError):
            Credentials.from_url(url + str(now - 1))

    def test_malformed_rsa_is_rejected(self):
        with self.assertRaises(UpstreamError):
            decode_address("this is not base64")
        self.assertEqual(decode_address(BASE), BASE)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_refresh_uses_one_discovery(self):
        r = Resolver(None, Config())
        r.discover = AsyncMock(return_value=credentials())
        result = await asyncio.gather(*(r.get() for _ in range(10)))
        self.assertEqual(r.discover.await_count, 1)
        self.assertTrue(all(t is result[0] for t in result))

    async def test_refresh_error_has_cooldown_and_uses_still_valid_token(self):
        r = Resolver(None, Config())
        old = r.cached = credentials(life=60)
        r.discover = AsyncMock(side_effect=UpstreamError("upstream unavailable"))
        self.assertIs(await r.get(), old)
        self.assertIs(await r.get(), old)
        self.assertEqual(r.discover.await_count, 1)
        with self.assertRaises(UpstreamError):
            await r.get(force=True, rejected=old)

    async def test_expired_token_never_used_on_refresh_failure(self):
        r = Resolver(None, Config())
        r.cached = credentials(life=-1)
        r.discover = AsyncMock(side_effect=UpstreamError("no token"))
        with self.assertRaises(UpstreamError):
            await r.get()

    async def test_other_channel_donor_is_rejected_and_scan_continues(self):
        r = Resolver(None, Config(donor_ids=(1,), scan_days=1))
        good = credentials()
        payload = base64.urlsafe_b64encode(json.dumps({"exp": good.expires}).encode()).decode().rstrip("=")
        url = BASE + f"?token=h.{payload}.s&volcSecret=x&volcTime={int(good.expires)}"
        calls = []
        async def api(path, params):
            calls.append(params)
            if path.endswith("/programs"):
                return {"programs": [{"id": 2, "is_review": 1, "name": "体育新闻"}]}
            return {"channel_info": {"id": "1" if params["channel_program_id"] == 1 else "10", "shift_address": url}}
        r.api = api
        self.assertEqual((await r.discover()).stream, "sports")
        self.assertEqual(r.preferred_donor, 2)
        calls.clear()
        await r.discover()
        self.assertEqual(calls, [{"channel_program_id": 2}])


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        async def upstream(request):
            self.calls.append((request.path, dict(request.query), request.headers.get("Range"), request.method))
            if request.path.endswith("index.m3u8"):
                return web.Response(text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvariant.m3u8?token=old&volcSecret=s&volcTime=1\n')
            if request.path.endswith("variant.m3u8"):
                return web.Response(text='#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:1\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6,\nsegment.ts\n')
            if request.path.endswith("key.bin"):
                return web.Response(body=b"0123456789abcdef", content_type="application/octet-stream")
            if request.path.endswith("init.mp4"):
                return web.Response(body=b"init-data", content_type="video/mp4")
            if request.headers.get("Range") == "bytes=2-5":
                return web.Response(status=206, body=b"2345", headers={"Content-Range": "bytes 2-5/10", "Accept-Ranges": "bytes"}, content_type="video/mp2t")
            return web.Response(body=b"0123456789", content_type="video/mp2t")
        fixture = web.Application()
        fixture.router.add_get("/{path:.*}", upstream)
        self.origin = TestServer(fixture)
        await self.origin.start_server()
        self.session = aiohttp.ClientSession()
        self.resolver = Resolver(None, Config())
        self.resolver.cached = credentials()
        self.resolver.discover = AsyncMock(return_value=credentials("new"))
        resources = Resources((HOST,))
        config = Config(access_token="house")
        origin, session = self.origin, self.session
        class LocalProxy(Proxy):
            async def open(self, url, *, method="GET", headers=None, decompress=False):
                validate_url(url, self.config.allowed_hosts)
                p = urlsplit(url)
                return await session.request(method, origin.make_url(p.path + ("?" + p.query if p.query else "")), headers=headers or {}, auto_decompress=decompress)
        # Restore the fixture response URL to its logical HTTPS origin for relative URI rewriting.
        class FixtureResources(Resources):
            def register(inner, url, playlist=False):
                p = urlsplit(url)
                return super().register(f"https://{HOST}" + p.path + ("?" + p.query if p.query else ""), playlist)
        self.resources = FixtureResources((HOST,))
        proxy = LocalProxy(self.session, self.resolver, self.resources, config)
        self.client = TestClient(TestServer(create_app(config, resolver=self.resolver, proxy=proxy, resources=self.resources)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.session.close()
        await self.origin.close()

    async def test_phone_to_master_variant_key_map_segment_and_range(self):
        response = await self.client.get("/live.m3u8?key=house")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("application/vnd.apple.mpegurl"))
        master = await response.text()
        variant_path = [x for x in master.splitlines() if x.startswith("/hls/")][0]
        variant = await self.client.get(variant_path)
        text = await variant.text()
        for path, expected in zip(re.findall(r'URI="([^"]+)"', text), [b"0123456789abcdef", b"init-data"]):
            self.assertEqual(await (await self.client.get(path)).read(), expected)
        segment = [x for x in text.splitlines() if x.startswith("/hls/")][0]
        part = await self.client.get(segment, headers={"Range": "bytes=2-5"})
        self.assertEqual(part.status, 206)
        self.assertEqual(part.headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(await part.read(), b"2345")
        head = await self.client.head(segment)
        self.assertEqual(head.status, 200)
        self.assertEqual(head.headers["Content-Length"], "10")
        self.assertEqual(await head.read(), b"")
        self.assertNotIn("volcSecret", text)
        # A TV may keep requesting its selected variant, without reloading the master.
        self.resolver.cached = credentials(life=30)
        again = await self.client.get(variant_path)
        self.assertEqual(again.status, 200)
        self.assertEqual(self.resolver.discover.await_count, 1)
        self.assertEqual(self.calls[-1][1]["token"], "new")

    async def test_access_key_and_liveness_and_static_assets(self):
        for path in ("/", "/live.m3u8", "/api/status"):
            self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.get("/healthz")).status, 200)
        self.assertEqual((await self.client.get("/static/app.js")).status, 200)
        self.assertEqual((await self.client.get("/?key=house")).status, 200)
        self.assertEqual((await self.client.get("/hls/missing/media?key=house")).status, 410)
        self.assertEqual(self.calls, [])

    async def test_failed_source_returns_redacted_error(self):
        self.resolver.cached = None
        self.resolver.discover = AsyncMock(side_effect=UpstreamError("No usable playback token was found."))
        response = await self.client.get("/live.m3u8?key=house")
        self.assertEqual(response.status, 502)
        self.assertIn("No usable", (await response.json())["error"])


if __name__ == "__main__":
    unittest.main()
