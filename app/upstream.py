"""SMG API adapter. No user credentials or private keys are embedded here.

Protocol constants and API flow were inspected in jolin1314joker/SMG_TV v0.20.
The public RSA operation recovers the address wrapping used by the web client;
it does not generate/validate JWT signatures or decrypt DRM-protected media.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from .config import Config
from .endpoints import API_BASE, STREAM_BASE

LOG = logging.getLogger(__name__)
AUTH_PARAMS = frozenset(("token", "volcSecret", "volcTime"))
# Modulus/exponent extracted from the public PEM embedded in the source.
PUBLIC_N = int("cfe61ccf516e5115e136c414f5111077847648568b67fea6ad5a181cd5e6687f4f6a2a312514de8d99ae3ad590301a95f869ecca3fc01d8785898f8bb63b9e310970edc33291a993b6a0d664b8d985d956bc90b82211000073161cf0981337eb9040da6c7a9e27fe8d6c02b4c9a28648175ec4b52a928170dc27bc838f9adcef", 16)


class UpstreamError(Exception):
    """A safe, redacted error suitable for display to the user."""


async def read_limited(response, limit):
    data = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > limit:
            raise UpstreamError("The upstream response is unexpectedly large.")
    return bytes(data)


def sign_params(params, config, *, timestamp=None, nonce=None):
    merged = {
        **params,
        "platform": "pc", "version": config.api_version,
        "nonce": nonce or secrets.token_hex(4),
        "timestamp": int(time.time()) if timestamp is None else timestamp,
        "Api-Version": "v1",
    }
    data = "".join(f"{k}={merged[k]}&" for k in sorted(merged) if merged[k] is not None)
    digest = hashlib.md5((data + config.api_secret).encode()).hexdigest()
    merged["sign"] = hashlib.md5(digest.encode()).hexdigest()
    return {k: str(v) for k, v in merged.items()}


def decode_address(value, *, modulus=PUBLIC_N, exponent=65537):
    if not isinstance(value, str):
        raise UpstreamError("The API returned an invalid playback address.")
    if value.startswith("https://"):
        return value
    try:
        raw = base64.b64decode(value, validate=True)
        block_size = (modulus.bit_length() + 7) // 8
        if not raw or len(raw) % block_size:
            raise ValueError("RSA block length")
        pieces = []
        for i in range(0, len(raw), block_size):
            n = int.from_bytes(raw[i:i + block_size], "big")
            if n >= modulus:
                raise ValueError("RSA block value")
            block = pow(n, exponent, modulus).to_bytes(block_size, "big")
            # The site has used PKCS#1 wrapping recovered with its public key.
            if block[:2] not in (b"\x00\x01", b"\x00\x02"):
                raise ValueError("RSA padding type")
            split = block.index(b"\x00", 2)
            if split < 10 or (block[1] == 1 and any(b != 255 for b in block[2:split])):
                raise ValueError("RSA padding")
            pieces.append(block[split + 1:])
        result = b"".join(pieces).decode("utf-8")
        if not result.startswith("https://"):
            raise ValueError("Address scheme")
        return result
    except (ValueError, UnicodeError) as exc:
        raise UpstreamError("Playback address decoding failed; the upstream protocol may have changed.") from exc


@dataclass(frozen=True, repr=False)
class Credentials:
    stream: str
    token: str
    volc_secret: str
    volc_time: str
    expires: float

    @classmethod
    def from_url(cls, value):
        try:
            url = urlsplit(value)
            match = re.search(r"/live/([^/]+)/", url.path)
            q = dict(parse_qsl(url.query))
            part = q["token"].split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            if not match or not q.get("volcSecret") or not q.get("volcTime"):
                raise ValueError("Missing stream credentials")
            # The CDN signature can expire before the JWT (observed: 10 min vs 12 h).
            exp = min(float(payload["exp"]), int(q["volcTime"]))
            if exp <= time.time() + 15:
                raise ValueError("Expired token")
            return cls(match[1], q["token"], q["volcSecret"], q["volcTime"], exp)
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError) as exc:
            raise UpstreamError("The playback token is missing, invalid, or expired.") from exc

    def auth(self):
        return {"token": self.token, "volcSecret": self.volc_secret, "volcTime": self.volc_time}

    def live_url(self):
        return f"{STREAM_BASE}/live/{self.stream}/index.m3u8?{urlencode(self.auth())}"

    def update_url(self, url):
        """Refresh only credential fields already present in a playlist URI."""
        parts = urlsplit(url)
        auth = self.auth()
        pairs = [(k, auth[k] if k in auth else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit(parts._replace(query=urlencode(pairs)))


class Resolver:
    def __init__(self, session, config: Config):
        self.session = session
        self.config = config
        self.cached = None
        self.lock = asyncio.Lock()
        self.api_lock = asyncio.Lock()
        self.last_request = 0.0
        self.retry_after = 0.0
        self.last_error = None
        self.last_success = None
        self.preferred_donor = None

    async def api(self, path, params):
        async with self.api_lock:
            await asyncio.sleep(max(0, self.config.request_interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            headers = sign_params(params, self.config)
            headers.update({"M-Uuid": self.config.uuid, "Accept": "application/json"})
            try:
                async with self.session.get(API_BASE + path, params=params, headers=headers,
                                            allow_redirects=False, auto_decompress=True,
                                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 404:
                        return {}
                    if resp.status != 200:
                        raise UpstreamError(f"SMG API returned HTTP {resp.status}.")
                    if resp.content_length and resp.content_length > 2_000_000:
                        raise UpstreamError("SMG API response is unexpectedly large.")
                    body = await read_limited(resp, 2_000_000)
                    data = json.loads(body)
                    if isinstance(data, dict) and data.get("code") in (404, "404"):
                        return {}
                    if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
                        raise UpstreamError("SMG API did not return programme data; its signature or response format may have changed.")
                    return data["result"]
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                raise UpstreamError("Could not read the SMG API. Check network access and the server clock.") from exc

    async def get(self, *, force=False, rejected=None):
        now = time.time()
        if not force and self.cached and self.cached.expires - now > self.config.refresh_margin:
            return self.cached
        async with self.lock:
            now = time.time()
            if self.cached and ((not force and self.cached.expires - now > self.config.refresh_margin)
                                or (force and rejected is not None and self.cached is not rejected)):
                return self.cached
            if now < self.retry_after:
                if not force and self.cached and self.cached.expires > now + 10:
                    return self.cached
                raise UpstreamError(self.last_error or "Upstream refresh is cooling down; retry in a minute.")
            try:
                credential = await asyncio.wait_for(self.discover(), timeout=self.config.lookup_timeout)
                self.cached = credential
                self.last_success = time.time()
                self.last_error = None
                self.retry_after = 0
                LOG.info("Playback credentials refreshed for channel %s", self.config.channel_id)
                return credential
            except (UpstreamError, asyncio.TimeoutError) as exc:
                self.last_error = str(exc) if isinstance(exc, UpstreamError) else "SMG credential lookup timed out; retry in a minute."
                self.retry_after = time.time() + 60
                LOG.warning("%s", self.last_error)
                if not force and self.cached and self.cached.expires > time.time() + 10:
                    return self.cached
                raise UpstreamError(self.last_error) from exc

    async def discover(self):
        tried = set()

        async def attempt(program_id):
            if program_id in tried or len(tried) >= self.config.max_donors:
                return None
            tried.add(program_id)
            detail = await self.api("/content/pc/tv/program/detail", {"channel_program_id": program_id})
            channel = detail.get("channel_info") or {}
            returned_id = channel.get("id", channel.get("channel_id"))
            # Fail closed when the API cannot confirm the donor's channel.
            if returned_id is None or str(returned_id) != self.config.channel_id:
                return None
            for name in ("shift_address", "live_address"):
                if not channel.get(name):
                    continue
                try:
                    credentials = Credentials.from_url(decode_address(channel[name]))
                except UpstreamError:
                    continue
                expected = self.config.expected_stream or (self.cached.stream if self.cached else "")
                if expected and credentials.stream != expected:
                    continue
                self.preferred_donor = program_id
                return credentials
            return None

        initial = ([self.preferred_donor] if self.preferred_donor else []) + list(self.config.donor_ids)
        for program_id in dict.fromkeys(initial):
            found = await attempt(program_id)
            if found:
                return found
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for offset in range(self.config.scan_days):
            date = (today - timedelta(days=offset)).isoformat()
            result = await self.api("/content/pc/tv/programs", {"channel_id": self.config.channel_id, "date": date})
            programs = result.get("programs", [])
            if not isinstance(programs, list):
                continue
            # The userscript sets the page's review flags before selecting donors.
            # A sports-news detail can therefore be a candidate even when its list flag is zero.
            candidates = [p for p in programs if isinstance(p, dict) and p.get("id")
                          and (p.get("is_review") in (1, "1") or "体育新闻" in str(p.get("name", "")))
                          and str(p.get("channel_id") or self.config.channel_id) == self.config.channel_id]
            candidates.sort(key=lambda p: ("体育新闻" not in str(p.get("name", "")), p.get("is_review") not in (1, "1")))
            for program in candidates:
                found = await attempt(program["id"])
                if found:
                    return found
                if len(tried) >= self.config.max_donors:
                    break
            if len(tried) >= self.config.max_donors:
                break
        raise UpstreamError("No usable playback token was found for this channel. The upstream access method may no longer work.")

    def status(self):
        return {
            "channel_id": self.config.channel_id,
            "credentials_ready": bool(self.cached and self.cached.expires > time.time()),
            "token_seconds_left": max(0, int(self.cached.expires - time.time())) if self.cached else 0,
            "last_error": self.last_error,
            "last_success": self.last_success,
        }
